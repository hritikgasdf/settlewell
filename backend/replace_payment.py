"""One replacement per attempt. The request id is shared by concurrent callers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.airwallex.client import AirwallexError
from backend.ledger import set_obligation_status
from backend.policy import Action, code_from_reason, decide

_DEFINITIVE_REFUSAL = frozenset(
    {"insufficient_funds", "validation_failed", "amount_above_limit", "limit_exceeded"}
)
_MAX_ATTEMPTS = 3
_DEDUPE_MARGIN = timedelta(days=6)


class ReplaceError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def replacement_request_id(invoice_ref: str, attempt: int) -> str:
    slug = "".join(character for character in invoice_ref.lower() if character.isalnum())
    request_id = f"stl-{slug}-{attempt}"
    if not 10 <= len(request_id) <= 50:
        raise ReplaceError("request_id")
    return request_id


def replace_payment(
    conn,
    client,
    *,
    obligation_id: int,
    attempt: int,
    beneficiary_id: str,
    details_hash: str,
    transfer_method: str,
    reason: str,
    as_of: str | None = None,
    transfer_amount: Decimal | None = None,
    transfer_currency: str | None = None,
    intent_id: int | None = None,
    approval_id: int | None = None,
    quote_id: str | None = None,
) -> str:
    """Create at most one transfer for this attempt. Amount and currency come from the obligation."""
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if obligation is None:
        raise ReplaceError("missing")
    if _ambiguous(conn, obligation_id):
        raise ReplaceError("ambiguous")
    if attempt > _MAX_ATTEMPTS:
        raise ReplaceError("max_attempts")
    _require_prior_refusal(conn, obligation["invoice_ref"], attempt)
    request_id = replacement_request_id(obligation["invoice_ref"], attempt)
    if _by_request_id(conn, request_id) is None and _past_dedupe_margin(conn, obligation_id, as_of):
        raise ReplaceError("too_late")
    recorded = False
    conn.execute("BEGIN IMMEDIATE")
    try:
        amount = transfer_amount if transfer_amount is not None else Decimal(obligation["amount"])
        currency = transfer_currency or obligation["currency"]
        if intent_id is None or approval_id is None or as_of is None:
            conn.rollback()
            raise ReplaceError("approval")
        existing = _by_request_id(conn, request_id)
        if existing is not None:
            _require_owner(
                conn,
                intent_id=intent_id,
                approval_id=approval_id,
                obligation_id=obligation_id,
                as_of=as_of,
                amount=format(amount, "f"),
                currency=currency,
                beneficiary_id=beneficiary_id,
                details_hash=details_hash,
                quote_id=quote_id,
                request_id=request_id,
                live=bool(existing["airwallex_id"]),
            )
        if existing is not None and existing["airwallex_id"]:
            from backend.execute import remember_remote

            remember_remote(conn, intent_id, str(existing["airwallex_id"]), existing["status"] or "sent")
            conn.commit()
            return str(existing["airwallex_id"])
        if existing is not None:
            conn.commit()
            recorded = True
            return _recover_recorded(
                conn, client, obligation, request_id, intent_id, details_hash, amount, currency
            )
        _refuse_unless_replaceable(conn, client, obligation)
        from backend import funding
        from backend.execute import ExecuteError, claim, remember_remote

        def gate() -> None:
            funding.gate(
                conn, client, obligation=obligation, intent_id=intent_id, request_id=request_id,
                amount=amount, currency=currency, as_of=as_of,
            )

        try:
            claimed = claim(
                conn,
                intent_id=intent_id,
                approval_id=approval_id,
                obligation_id=obligation_id,
                as_of=as_of,
                kind="replacement",
                amount=format(amount, "f"),
                currency=currency,
                beneficiary_id=beneficiary_id,
                details_hash=details_hash,
                quote_id=quote_id,
                request_id=request_id,
                before_persist=gate,
            )
        except ExecuteError as error:
            conn.rollback()
            raise ReplaceError(error.code) from error
        if claimed.remote_id:
            conn.commit()
            return claimed.remote_id
        if not claimed.fresh:
            conn.commit()
            recorded = True
            return _recover_recorded(
                conn, client, obligation, request_id, intent_id, details_hash, amount, currency
            )
        if _other_live(conn, obligation_id, request_id):
            conn.rollback()
            raise ReplaceError("live")
        if _past_dedupe_margin(conn, obligation_id, as_of):
            conn.rollback()
            raise ReplaceError("too_late")
        _mark(
            conn,
            obligation,
            request_id,
            details_hash,
            status="IN_FLIGHT",
            failure_code=None,
            amount=format(amount, "f"),
            currency=currency,
        )
        conn.commit()
        recorded = True
        remote = _create_or_find(
            client,
            obligation,
            request_id=request_id,
            beneficiary_id=beneficiary_id,
            transfer_method=transfer_method,
            reason=reason,
            amount=amount,
            currency=currency,
        )
        _store(
            conn,
            obligation,
            remote,
            request_id=request_id,
            details_hash=details_hash,
            amount=format(amount, "f"),
            currency=currency,
        )
        remember_remote(conn, intent_id, str(remote.id), remote.status)
        funding.record_acceptance(conn, request_id, remote, as_of)
        conn.commit()
    except ReplaceError as error:
        if error.code == "unknown":
            if not recorded:
                _mark(conn, obligation, request_id, details_hash, status="IN_FLIGHT", failure_code=None)
            set_obligation_status(conn, obligation_id, "IN_FLIGHT")
            conn.commit()
        elif not recorded:
            conn.rollback()
        raise
    except AirwallexError as error:
        if error.code in _DEFINITIVE_REFUSAL:
            _mark(conn, obligation, request_id, details_hash, status="REFUSED", failure_code=error.code)
            if recorded:
                from backend import funding

                funding.release_refused(conn, request_id, as_of)
            conn.commit()
            raise ReplaceError("refused") from error
        if recorded:
            set_obligation_status(conn, obligation_id, "IN_FLIGHT")
            conn.commit()
        else:
            conn.rollback()
        raise
    except Exception:
        if recorded:
            set_obligation_status(conn, obligation_id, "IN_FLIGHT")
            conn.commit()
        else:
            conn.rollback()
        raise
    funding.observe_after(conn, client, request_id, as_of)
    if remote.status in {"FAILED", "CANCELLED"}:
        set_obligation_status(conn, obligation_id, "IN_FLIGHT")
        conn.commit()
        raise ReplaceError("person")
    return str(remote.id)


def _refuse_unless_replaceable(conn, client, obligation) -> None:
    """A recall or any non-replace code never creates a payout, even with a grant."""
    live_id = obligation["live_transfer_id"]
    original = None
    if live_id is not None:
        original = conn.execute(
            "SELECT airwallex_id FROM transfer_record WHERE id = ?",
            (live_id,),
        ).fetchone()
    if original is None or not original["airwallex_id"]:
        raise ReplaceError("policy")
    remote = client.get_transfer(original["airwallex_id"])
    decision = decide(
        remote.status,
        failure_code=code_from_reason(remote.failure_reason),
        claim="where",
    )
    if decision.action not in {Action.REPLACE_SAME, Action.REPLACE_NEW_DETAILS}:
        raise ReplaceError("policy")


def _create_or_find(
    client,
    obligation,
    *,
    request_id,
    beneficiary_id,
    transfer_method,
    reason,
    amount: Decimal,
    currency: str,
):
    try:
        return client.create_transfer(
            transfer_amount=amount,
            transfer_currency=currency,
            source_currency=currency,
            beneficiary_id=beneficiary_id,
            transfer_method=transfer_method,
            reference=obligation["invoice_ref"],
            reason=reason,
            request_id=request_id,
            metadata={"obligation_id": str(obligation["id"])},
        )
    except AirwallexError as error:
        if error.code != "duplicate_request_id":
            raise
        return _lookup(client, request_id)
    except TimeoutError:
        return _lookup(client, request_id)


def _lookup(client, request_id: str):
    found = _lookup_optional(client, request_id)
    if found is None:
        raise ReplaceError("unknown")
    return found


def _lookup_optional(client, request_id: str):
    try:
        page = client.list_transfers(request_id=request_id)
    except (AirwallexError, TimeoutError):
        raise ReplaceError("unknown") from None
    if len(page.items) != 1:
        return None
    return page.items[0]


def _require_owner(
    conn,
    *,
    intent_id,
    approval_id,
    obligation_id,
    as_of,
    amount,
    currency,
    beneficiary_id,
    details_hash,
    quote_id,
    request_id,
    live: bool,
) -> None:
    """Read-only. Only the executed intent that owns this request id may recover it."""
    from backend.execute import ExecuteError, claim

    owner = conn.execute(
        "SELECT id FROM execution_intent WHERE id = ? AND state = 'executed' AND request_id = ?",
        (intent_id, request_id),
    ).fetchone()
    if owner is None:
        conn.rollback()
        raise ReplaceError("live" if live else "approval")
    try:
        claimed = claim(
            conn,
            intent_id=intent_id,
            approval_id=approval_id,
            obligation_id=obligation_id,
            as_of=as_of,
            kind="replacement",
            amount=amount,
            currency=currency,
            beneficiary_id=beneficiary_id,
            details_hash=details_hash,
            quote_id=quote_id,
            request_id=request_id,
        )
    except ExecuteError as error:
        conn.rollback()
        raise ReplaceError(error.code) from error
    if claimed.fresh:
        conn.rollback()
        raise ReplaceError("approval")


def _recover_recorded(conn, client, obligation, request_id, intent_id, details_hash, amount: Decimal, currency: str) -> str:
    """Look up the stable request id before any retry. Does not create a transfer."""
    from backend.execute import remember_remote

    remote = _lookup_optional(client, request_id)
    if remote is None:
        set_obligation_status(conn, obligation["id"], "IN_FLIGHT")
        conn.commit()
        raise ReplaceError("in_flight")
    from backend import funding

    intent = conn.execute("SELECT beneficiary_id FROM execution_intent WHERE id = ?", (intent_id,)).fetchone()
    if intent is None or not funding.terms_match(
        remote,
        request_id=request_id,
        invoice_ref=obligation["invoice_ref"],
        amount=amount,
        currency=currency,
        beneficiary_id=intent["beneficiary_id"],
    ):
        # Wrong or foreign transfer: bind nothing, write nothing for it, never resend.
        set_obligation_status(conn, obligation["id"], "IN_FLIGHT")
        conn.commit()
        raise ReplaceError("in_flight")
    _store(
        conn,
        obligation,
        remote,
        request_id=request_id,
        details_hash=details_hash,
        amount=format(amount, "f"),
        currency=currency,
    )
    remember_remote(conn, intent_id, str(remote.id), remote.status)
    from backend.funding import record_acceptance

    record_acceptance(conn, request_id, remote, datetime.now(UTC).isoformat())
    conn.commit()
    return str(remote.id)


def same_details_target(conn, obligation, remote) -> tuple[str, str]:
    """Onboarding beneficiary for REPLACE_SAME. A call-back version is not this path."""
    version = conn.execute(
        """
        SELECT beneficiary_id, details_hash FROM bank_detail_version
        WHERE supplier_id = ? AND source = 'onboarding'
        ORDER BY id ASC LIMIT 1
        """,
        (obligation["supplier_id"],),
    ).fetchone()
    if version is None:
        raise ReplaceError("unverified")
    onboarding_id = version["beneficiary_id"]
    remote_id = remote.beneficiary_id
    if onboarding_id and remote_id and onboarding_id != remote_id:
        raise ReplaceError("beneficiary")
    beneficiary_id = onboarding_id or remote_id
    if not beneficiary_id:
        raise ReplaceError("beneficiary")
    return str(beneficiary_id), str(version["details_hash"])


def _ambiguous(conn, obligation_id: int) -> bool:
    row = conn.execute(
        "SELECT id FROM incident WHERE obligation_id = ? AND state = 'AMBIGUOUS'",
        (obligation_id,),
    ).fetchone()
    return row is not None


def _require_prior_refusal(conn, invoice_ref: str, attempt: int) -> None:
    if attempt <= 1:
        return
    previous = _by_request_id(conn, replacement_request_id(invoice_ref, attempt - 1))
    if previous is None or previous["status"] != "REFUSED":
        raise ReplaceError("refused_sequence")


def _past_dedupe_margin(conn, obligation_id: int, as_of: str | None) -> bool:
    """Six days from the first snapshot. Later snapshots do not move the cutoff. The clock is real when as_of is omitted."""
    row = conn.execute(
        """
        SELECT snapshot.seen_at
        FROM status_snapshot AS snapshot
        JOIN transfer_record ON transfer_record.id = snapshot.transfer_record_id
        WHERE transfer_record.obligation_id = ?
        ORDER BY snapshot.seen_at ASC
        LIMIT 1
        """,
        (obligation_id,),
    ).fetchone()
    if row is None:
        return True
    return _clock(as_of) - _clock(row["seen_at"]) > _DEDUPE_MARGIN


def _clock(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _other_live(conn, obligation_id: int, request_id: str) -> bool:
    """A second in-flight payment for the same invoice is refused."""
    row = conn.execute(
        """
        SELECT id FROM transfer_record
        WHERE obligation_id = ?
          AND origin = 'settlewell'
          AND status IN ('SCHEDULED', 'PROCESSING', 'SENT', 'PAID', 'OVERDUE')
          AND COALESCE(request_id, '') != ?
        """,
        (obligation_id, request_id),
    ).fetchone()
    return row is not None


def _by_request_id(conn, request_id: str):
    return conn.execute(
        "SELECT airwallex_id, status FROM transfer_record WHERE request_id = ?",
        (request_id,),
    ).fetchone()


def _mark(
    conn,
    obligation,
    request_id: str,
    details_hash: str,
    *,
    status: str,
    failure_code: str | None,
    amount: str | None = None,
    currency: str | None = None,
) -> None:
    existing = _by_request_id(conn, request_id)
    if existing is not None:
        if existing["airwallex_id"]:
            return
        conn.execute(
            "UPDATE transfer_record SET status = ?, failure_code = ? WHERE request_id = ?",
            (status, failure_code, request_id),
        )
        return
    conn.execute(
        """
        INSERT INTO transfer_record (
            request_id, obligation_id, origin, details_hash, amount, currency,
            status, failure_code, reference
        ) VALUES (?, ?, 'settlewell', ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            obligation["id"],
            details_hash,
            amount or obligation["amount"],
            currency or obligation["currency"],
            status,
            failure_code,
            obligation["invoice_ref"],
        ),
    )


def _store(conn, obligation, remote, *, request_id, details_hash, amount: str, currency: str) -> None:
    existing = _by_request_id(conn, request_id)
    if existing is not None:
        if not existing["airwallex_id"]:
            conn.execute(
                """
                UPDATE transfer_record
                SET airwallex_id = ?, status = ?, amount = ?, currency = ?
                WHERE request_id = ?
                """,
                (remote.id, remote.status, amount, currency, request_id),
            )
        return
    conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, obligation_id, origin, details_hash,
            amount, currency, status, reference
        ) VALUES (?, ?, ?, 'settlewell', ?, ?, ?, ?, ?)
        """,
        (
            remote.id,
            request_id,
            obligation["id"],
            details_hash,
            amount,
            currency,
            remote.status,
            obligation["invoice_ref"],
        ),
    )

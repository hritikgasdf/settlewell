"""One replacement per attempt. The request id is shared by concurrent callers."""

from __future__ import annotations

from datetime import datetime, timedelta
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
    if attempt > _MAX_ATTEMPTS:
        raise ReplaceError("max_attempts")
    if _past_dedupe_margin(conn, obligation_id, as_of):
        raise ReplaceError("too_late")
    _require_prior_refusal(conn, obligation["invoice_ref"], attempt)
    request_id = replacement_request_id(obligation["invoice_ref"], attempt)
    conn.execute("BEGIN IMMEDIATE")
    try:
        amount = transfer_amount if transfer_amount is not None else Decimal(obligation["amount"])
        currency = transfer_currency or obligation["currency"]
        if intent_id is None or approval_id is None or as_of is None:
            conn.rollback()
            raise ReplaceError("approval")
        _refuse_unless_replaceable(conn, client, obligation)
        from backend.execute import ExecuteError, claim, remember_remote

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
            )
        except ExecuteError as error:
            conn.rollback()
            raise ReplaceError(error.code) from error
        if claimed.remote_id:
            conn.commit()
            return claimed.remote_id
        if not claimed.fresh:
            existing = _by_request_id(conn, request_id)
            if existing is not None and existing["airwallex_id"]:
                remember_remote(conn, intent_id, str(existing["airwallex_id"]), "sent")
                conn.commit()
                return str(existing["airwallex_id"])
            conn.rollback()
            raise ReplaceError("in_flight")
        existing = _by_request_id(conn, request_id)
        if existing is not None or _other_live(conn, obligation_id, request_id):
            conn.rollback()
            raise ReplaceError("live")
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
        conn.commit()
    except ReplaceError as error:
        if error.code == "unknown":
            _mark(conn, obligation, request_id, details_hash, status="IN_FLIGHT", failure_code=None)
            set_obligation_status(conn, obligation_id, "IN_FLIGHT")
            conn.commit()
        else:
            conn.rollback()
        raise
    except AirwallexError as error:
        if error.code in _DEFINITIVE_REFUSAL:
            _mark(conn, obligation, request_id, details_hash, status="REFUSED", failure_code=error.code)
            conn.commit()
            raise ReplaceError("refused") from error
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
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
    page = client.list_transfers(request_id=request_id)
    if len(page.items) != 1:
        raise ReplaceError("unknown")
    return page.items[0]


def _require_prior_refusal(conn, invoice_ref: str, attempt: int) -> None:
    if attempt <= 1:
        return
    previous = _by_request_id(conn, replacement_request_id(invoice_ref, attempt - 1))
    if previous is None or previous["status"] != "REFUSED":
        raise ReplaceError("refused_sequence")


def _past_dedupe_margin(conn, obligation_id: int, as_of: str | None) -> bool:
    if as_of is None:
        return False
    row = conn.execute(
        """
        SELECT snapshot.seen_at
        FROM status_snapshot AS snapshot
        JOIN transfer_record ON transfer_record.id = snapshot.transfer_record_id
        WHERE transfer_record.obligation_id = ?
        ORDER BY snapshot.seen_at DESC
        LIMIT 1
        """,
        (obligation_id,),
    ).fetchone()
    if row is None:
        return False
    failed_on = datetime.fromisoformat(row["seen_at"])
    now = datetime.fromisoformat(as_of)
    return now - failed_on > _DEDUPE_MARGIN


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


def _mark(conn, obligation, request_id: str, details_hash: str, *, status: str, failure_code: str | None) -> None:
    if _by_request_id(conn, request_id) is not None:
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
            obligation["amount"],
            obligation["currency"],
            status,
            failure_code,
            obligation["invoice_ref"],
        ),
    )


def _store(conn, obligation, remote, *, request_id, details_hash, amount: str, currency: str) -> None:
    if _by_request_id(conn, request_id) is not None:
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

"""Intent, approval, then one remote action. P0-2 and P0-3 call this module.

Public surface:
- open_intent
- intent_terms
- execute
- claim
- CallbackConfirmation
- normalize_timestamp

Do not call Airwallex money or beneficiary APIs except from execute or from a
helper that has claimed an intent inside execute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from backend.approvals import (
    DEMO_IDENTITIES,
    ApprovalTerms,
    binding_hash,
    propose,
)
from backend.policy import Action, code_from_reason, decide

_REPLACE = {Action.REPLACE_SAME.value, Action.REPLACE_NEW_DETAILS.value}
_BENEFICIARY_KEYS = (
    "body",
    "bank_name",
    "account_last4",
    "phone",
    "confirmed",
    "attested_by",
    "attested_at",
)


class ExecuteError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CallbackConfirmation:
    """A call-back is a structured record, not a free-text note."""

    phone: str
    confirmed: bool
    attested_by: str
    attested_at: str


@dataclass(frozen=True)
class IntentRef:
    id: int
    approval_id: int
    kind: str


@dataclass(frozen=True)
class ExecuteResult:
    kind: str
    remote_id: str
    status: str
    funding_note: str | None = None


@dataclass(frozen=True)
class Claimed:
    """One intent after claim. fresh is false when this call recovered an earlier execution."""

    intent_id: int
    fresh: bool
    remote_id: str | None
    remote_status: str | None
    request_id: str | None


def normalize_timestamp(value: str) -> str:
    """UTC ISO-8601. Z and numeric offsets compare as the same instant."""
    if not isinstance(value, str) or not value.strip():
        raise ExecuteError("expiry")
    text = value.strip().replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and ":" not in text[-5:]:
        text = text[:-2] + ":" + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ExecuteError("expiry") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def open_intent(
    conn,
    *,
    kind: str,
    incident_id: int,
    obligation_id: int | None,
    action: str,
    amount: str,
    currency: str,
    beneficiary_id: str,
    details_hash: str,
    proposer: str,
    quote_id: str | None = None,
    target_id: str | None = None,
    failure_code: str | None = None,
    body_json: str | None = None,
    expires_at: str | None = None,
    created_at: str,
) -> IntentRef:
    """Persist the intent and the approval proposal. Does not call Airwallex."""
    if kind not in {"replacement", "fx_conversion", "beneficiary", "cancel"}:
        raise ExecuteError("kind")
    if obligation_id is None:
        raise ExecuteError("obligation")
    expiry = _expiry(expires_at)
    opened = _moment(created_at)
    cursor = conn.execute(
        """
        INSERT INTO execution_intent (
            kind, incident_id, obligation_id, action, amount, currency,
            beneficiary_id, details_hash, quote_id, target_id, failure_code,
            body_json, state, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
        """,
        (
            kind,
            incident_id,
            obligation_id,
            action,
            format(Decimal(amount), "f"),
            currency,
            beneficiary_id,
            details_hash,
            quote_id,
            target_id,
            failure_code,
            body_json,
            expiry,
            opened,
        ),
    )
    intent_id = int(cursor.lastrowid)
    approval_id = propose(
        conn,
        incident_id,
        _terms(conn, intent_id),
        proposer=proposer,
        expires_at=expiry,
    )
    conn.execute(
        "UPDATE execution_intent SET approval_id = ? WHERE id = ?",
        (approval_id, intent_id),
    )
    conn.commit()
    return IntentRef(intent_id, approval_id, kind)


def execute(conn, client, intent_id: int, *, as_of: str) -> ExecuteResult:
    """Run one approved intent. A second call returns the remote result it already stored."""
    intent = _intent(conn, intent_id)
    if intent["state"] == "executed":
        return _resume(conn, client, intent, as_of)
    _ready(conn, intent, as_of)
    if intent["kind"] == "replacement":
        _replacement_policy(conn, client, intent)
        from backend.replacement_tool import propose_replacement

        remote_id = propose_replacement(
            conn,
            client,
            obligation_id=int(intent["obligation_id"]),
            incident_ref=str(intent["incident_id"]),
            intent_id=int(intent["id"]),
            as_of=as_of,
        )
        return ExecuteResult("replacement", remote_id, "sent", _funding_note(conn, int(intent["id"])))
    if intent["kind"] == "fx_conversion":
        from backend.fx_quote import FxQuoteError, execute_fx_quote

        terms = _terms(conn, intent_id)
        try:
            decision = execute_fx_quote(
                conn,
                client,
                int(intent["approval_id"]),
                terms,
                as_of=as_of,
                intent_id=intent_id,
            )
        except FxQuoteError as error:
            raise ExecuteError(error.code) from error
        if decision.outcome != "converted" or not decision.conversion_id:
            raise ExecuteError(decision.outcome)
        return ExecuteResult("fx_conversion", decision.conversion_id, "converted")
    if intent["kind"] == "beneficiary":
        return _execute_beneficiary(conn, client, intent, as_of)
    if intent["kind"] == "cancel":
        from backend.stop_duplicate import stop_duplicate

        stopped = stop_duplicate(
            conn,
            client,
            approval_id=int(intent["approval_id"]),
            transfer_record_id=int(intent["target_id"]),
            as_of=as_of,
        )
        if not stopped.cancelled:
            raise ExecuteError("status_" + stopped.status)
        return ExecuteResult("cancel", str(intent["target_id"]), stopped.status)
    raise ExecuteError("kind")


def claim(
    conn,
    *,
    intent_id: int,
    approval_id: int,
    obligation_id: int,
    as_of: str,
    kind: str,
    amount: str,
    currency: str,
    beneficiary_id: str,
    details_hash: str,
    quote_id: str | None = None,
    target_id: str | None = None,
    request_id: str | None = None,
    before_persist=None,
) -> Claimed:
    """Take this intent, or recover it. Never selects another row with the same terms.

    before_persist runs after every exact-intent check and before the claim is stored."""
    intent = _intent(conn, intent_id)
    if int(intent["approval_id"] or 0) != int(approval_id):
        raise ExecuteError("approval")
    if int(intent["obligation_id"] or 0) != int(obligation_id):
        raise ExecuteError("obligation")
    if not _same_terms(
        intent,
        kind=kind,
        amount=amount,
        currency=currency,
        beneficiary_id=beneficiary_id,
        details_hash=details_hash,
        quote_id=quote_id,
        target_id=target_id,
    ):
        raise ExecuteError("void")
    if intent["state"] == "executed":
        return _claimed(intent, fresh=False)
    if intent["state"] != "proposed":
        raise ExecuteError("approval")
    _ready(conn, intent, as_of)
    if kind == "replacement" and not _code_may_pay(intent["failure_code"]):
        raise ExecuteError("policy")
    if before_persist is not None:
        before_persist()
    cursor = conn.execute(
        """
        UPDATE execution_intent
        SET state = 'executed', request_id = COALESCE(?, request_id)
        WHERE id = ? AND state = 'proposed'
        """,
        (request_id, intent_id),
    )
    if cursor.rowcount != 1:
        current = _intent(conn, intent_id)
        if current["state"] == "executed":
            return _claimed(current, fresh=False)
        raise ExecuteError("approval")
    stored = request_id or intent["request_id"]
    return Claimed(intent_id, True, None, None, stored)


def callback_ok(confirmation: CallbackConfirmation, *, phone_on_file: str | None, proposer: str) -> None:
    """Reject free text. The phone must match the file and confirmed must be True."""
    if not isinstance(confirmation, CallbackConfirmation):
        raise ExecuteError("callback")
    if confirmation.confirmed is not True:
        raise ExecuteError("callback")
    if not phone_on_file or confirmation.phone != phone_on_file:
        raise ExecuteError("callback")
    if confirmation.attested_by not in DEMO_IDENTITIES or confirmation.attested_by == proposer:
        raise ExecuteError("callback")
    try:
        normalize_timestamp(confirmation.attested_at)
    except ExecuteError:
        raise ExecuteError("callback") from None


def intent_terms(conn, intent_id: int) -> ApprovalTerms:
    """The approval terms bound to this intent. P0-2 approves these terms."""
    return _terms(conn, intent_id)


def remember_remote(conn, intent_id: int, remote_id: str, status: str) -> None:
    """Store the remote id on the intent that produced it."""
    conn.execute(
        """
        UPDATE execution_intent
        SET remote_id = ?, remote_status = ?
        WHERE id = ?
        """,
        (remote_id, status, intent_id),
    )


def _funding_note(conn, intent_id: int) -> str | None:
    from backend.funding import note_for_intent

    return note_for_intent(conn, intent_id)


def _resume(conn, client, intent, as_of: str) -> ExecuteResult:
    if intent["remote_id"] and intent["kind"] == "replacement":
        from backend.funding import backfill_after

        backfill_after(conn, client, int(intent["id"]), as_of)
        return ExecuteResult(
            "replacement",
            str(intent["remote_id"]),
            intent["remote_status"] or "recovered",
            _funding_note(conn, int(intent["id"])),
        )
    if intent["remote_id"]:
        return ExecuteResult(
            intent["kind"],
            str(intent["remote_id"]),
            intent["remote_status"] or "recovered",
        )
    if intent["kind"] == "beneficiary":
        return _execute_beneficiary(conn, client, intent, as_of)
    if intent["kind"] == "cancel":
        from backend.stop_duplicate import stop_duplicate

        stopped = stop_duplicate(
            conn,
            client,
            approval_id=int(intent["approval_id"]),
            transfer_record_id=int(intent["target_id"]),
            as_of=as_of,
        )
        if not stopped.cancelled:
            raise ExecuteError("status_" + stopped.status)
        return ExecuteResult("cancel", str(intent["target_id"]), stopped.status)
    if intent["kind"] == "replacement":
        from backend.replacement_tool import propose_replacement

        remote_id = propose_replacement(
            conn,
            client,
            obligation_id=int(intent["obligation_id"]),
            incident_ref=str(intent["incident_id"]),
            intent_id=int(intent["id"]),
            as_of=as_of,
        )
        from backend.funding import backfill_after

        backfill_after(conn, client, int(intent["id"]), as_of)
        return ExecuteResult("replacement", remote_id, "sent", _funding_note(conn, int(intent["id"])))
    if intent["kind"] == "fx_conversion":
        from backend.fx_quote import FxQuoteError, execute_fx_quote

        try:
            decision = execute_fx_quote(
                conn,
                client,
                int(intent["approval_id"]),
                _terms(conn, int(intent["id"])),
                as_of=as_of,
                intent_id=int(intent["id"]),
            )
        except FxQuoteError as error:
            raise ExecuteError(error.code) from error
        if not decision.conversion_id:
            raise ExecuteError(decision.outcome)
        return ExecuteResult("fx_conversion", decision.conversion_id, "converted")
    raise ExecuteError("kind")


def _execute_beneficiary(conn, client, intent, as_of: str) -> ExecuteResult:
    wrapped = _beneficiary_guard(conn, intent)
    request_id = intent["request_id"] or f"stl-ben-{int(intent['id'])}"
    claimed = claim(
        conn,
        intent_id=int(intent["id"]),
        approval_id=int(intent["approval_id"]),
        obligation_id=int(intent["obligation_id"]),
        as_of=as_of,
        kind="beneficiary",
        amount=intent["amount"],
        currency=intent["currency"],
        beneficiary_id=intent["beneficiary_id"],
        details_hash=intent["details_hash"],
        quote_id=intent["quote_id"],
        target_id=intent["target_id"],
        request_id=request_id,
    )
    conn.commit()
    if claimed.remote_id:
        return ExecuteResult("beneficiary", claimed.remote_id, claimed.remote_status or "created")
    if not claimed.fresh:
        found = _lookup_beneficiary(client, request_id)
        if found is None:
            raise ExecuteError("in_flight")
        _store_beneficiary(conn, intent, wrapped, found.id)
        remember_remote(conn, int(intent["id"]), found.id, "created")
        conn.commit()
        return ExecuteResult("beneficiary", found.id, "created")
    body = dict(wrapped["body"])
    body["request_id"] = request_id
    created = client.create_beneficiary(body)
    _store_beneficiary(conn, intent, wrapped, created.id)
    remember_remote(conn, int(intent["id"]), created.id, "created")
    conn.commit()
    return ExecuteResult("beneficiary", created.id, "created")


def _lookup_beneficiary(client, request_id: str):
    finder = getattr(client, "find_beneficiary", None)
    if finder is None:
        return None
    return finder(request_id)


def _store_beneficiary(conn, intent, wrapped: dict, remote_id: str) -> None:
    existing = conn.execute(
        """
        SELECT id FROM bank_detail_version
        WHERE beneficiary_id = ? AND details_hash = ? AND source = 'verified_callback'
        """,
        (remote_id, intent["details_hash"]),
    ).fetchone()
    if existing is not None:
        return
    conn.execute(
        """
        INSERT INTO bank_detail_version (
            supplier_id, beneficiary_id, details_hash, bank_name, account_last4,
            source, verified_by, verified_at
        )
        SELECT obligation.supplier_id, ?, ?, ?, ?,
               'verified_callback', ?, ?
        FROM obligation
        WHERE obligation.id = ?
        """,
        (
            remote_id,
            intent["details_hash"],
            wrapped["bank_name"],
            wrapped["account_last4"],
            wrapped["attested_by"],
            intent["created_at"],
            intent["obligation_id"],
        ),
    )


def _beneficiary_guard(conn, intent) -> dict:
    """The execute boundary. A hand-built body_json is not a call-back."""
    try:
        wrapped = json.loads(intent["body_json"] or "")
    except json.JSONDecodeError:
        raise ExecuteError("callback") from None
    if not isinstance(wrapped, dict) or any(key not in wrapped for key in _BENEFICIARY_KEYS):
        raise ExecuteError("callback")
    if wrapped["confirmed"] is not True or not isinstance(wrapped["body"], dict):
        raise ExecuteError("callback")
    approval = conn.execute(
        "SELECT proposer FROM approval WHERE id = ?",
        (intent["approval_id"],),
    ).fetchone()
    proposer = approval["proposer"] if approval is not None else ""
    confirmation = CallbackConfirmation(
        phone=str(wrapped["phone"]),
        confirmed=True,
        attested_by=str(wrapped["attested_by"]),
        attested_at=str(wrapped["attested_at"]),
    )
    phone = _phone_on_file(conn, int(intent["obligation_id"]))
    callback_ok(confirmation, phone_on_file=phone, proposer=proposer or "")
    from backend.bank_change import beneficiary_details_hash

    digest = beneficiary_details_hash(
        str(wrapped["bank_name"]),
        str(wrapped["account_last4"]),
        wrapped["body"],
    )
    if digest != intent["details_hash"]:
        raise ExecuteError("void")
    return wrapped


def _phone_on_file(conn, obligation_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT supplier.phone_on_file
        FROM obligation
        JOIN supplier ON supplier.id = obligation.supplier_id
        WHERE obligation.id = ?
        """,
        (obligation_id,),
    ).fetchone()
    if row is None:
        return None
    return row["phone_on_file"]


def _intent(conn, intent_id: int):
    row = conn.execute("SELECT * FROM execution_intent WHERE id = ?", (intent_id,)).fetchone()
    if row is None:
        raise ExecuteError("missing")
    return row


def _terms(conn, intent_id: int) -> ApprovalTerms:
    intent = _intent(conn, intent_id)
    return ApprovalTerms(
        action=intent["action"],
        amount=intent["amount"],
        currency=intent["currency"],
        beneficiary_id=intent["beneficiary_id"],
        details_hash=intent["details_hash"],
        quote_id=intent["quote_id"],
        evidence_hashes=(f"intent:{intent_id}",),
    )


def _same_terms(intent, *, kind: str, amount: str, currency: str, beneficiary_id: str, details_hash: str, quote_id: str | None, target_id: str | None) -> bool:
    return (
        intent["kind"] == kind
        and intent["amount"] == format(Decimal(amount), "f")
        and intent["currency"] == currency
        and intent["beneficiary_id"] == beneficiary_id
        and intent["details_hash"] == details_hash
        and (intent["quote_id"] or "") == (quote_id or "")
        and (intent["target_id"] or "") == (target_id or "")
    )


def _claimed(intent, *, fresh: bool) -> Claimed:
    return Claimed(
        int(intent["id"]),
        fresh,
        intent["remote_id"],
        intent["remote_status"],
        intent["request_id"],
    )


def _ready(conn, intent, as_of: str | None) -> None:
    if intent["approval_id"] is None:
        raise ExecuteError("unapproved")
    approval = conn.execute(
        "SELECT * FROM approval WHERE id = ?",
        (intent["approval_id"],),
    ).fetchone()
    if approval is None or approval["used_at"] is None:
        raise ExecuteError("unapproved")
    if approval["approver"] == approval["proposer"]:
        raise ExecuteError("self")
    expires = intent["expires_at"] or approval["expires_at"]
    if not expires:
        raise ExecuteError("expiry")
    if _moment(as_of) >= _expiry(expires):
        raise ExecuteError("expired")
    if binding_hash(_terms(conn, int(intent["id"]))) != approval["binding_hash"]:
        raise ExecuteError("void")


def _expiry(value: str | None) -> str:
    if value is None:
        raise ExecuteError("expiry")
    return normalize_timestamp(value)


def _moment(value: str | None) -> str:
    if value is None:
        raise ExecuteError("as_of")
    try:
        return normalize_timestamp(value)
    except ExecuteError:
        raise ExecuteError("as_of") from None


def _code_may_pay(failure_code: str | None) -> bool:
    decision = decide("CANCELLED", failure_code=failure_code, claim="where")
    return decision.action.value in _REPLACE


def _replacement_policy(conn, client, intent) -> None:
    obligation = conn.execute(
        "SELECT live_transfer_id FROM obligation WHERE id = ?",
        (intent["obligation_id"],),
    ).fetchone()
    if obligation is None or obligation["live_transfer_id"] is None:
        raise ExecuteError("policy")
    record = conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE id = ?",
        (obligation["live_transfer_id"],),
    ).fetchone()
    if record is None or not record["airwallex_id"]:
        raise ExecuteError("policy")
    remote = client.get_transfer(record["airwallex_id"])
    code = code_from_reason(remote.failure_reason)
    decision = decide(remote.status, failure_code=code, claim="where")
    if decision.action.value not in _REPLACE or not _code_may_pay(code):
        raise ExecuteError("policy")
    if intent["failure_code"] and intent["failure_code"] != code:
        raise ExecuteError("policy")

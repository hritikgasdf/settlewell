"""Intent, approval, then one remote action. P0-2 and P0-3 call this module.

Public surface:
- open_intent
- intent_terms
- execute
- claim
- CallbackConfirmation

Do not call Airwallex money or beneficiary APIs except from execute or from a
helper that has claimed an intent inside execute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from backend.approvals import (
    DEMO_IDENTITIES,
    ApprovalTerms,
    binding_hash,
    propose,
)
from backend.policy import Action, code_from_reason, decide

_REPLACE = {Action.REPLACE_SAME.value, Action.REPLACE_NEW_DETAILS.value}


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
            expires_at,
            created_at,
        ),
    )
    intent_id = int(cursor.lastrowid)
    approval_id = propose(
        conn,
        incident_id,
        _terms(conn, intent_id),
        proposer=proposer,
        expires_at=expires_at,
    )
    conn.execute(
        "UPDATE execution_intent SET approval_id = ? WHERE id = ?",
        (approval_id, intent_id),
    )
    conn.commit()
    return IntentRef(intent_id, approval_id, kind)


def execute(conn, client, intent_id: int, *, as_of: str) -> ExecuteResult:
    """Run one approved intent. Refuses when the grant is missing, expired, or the policy says no."""
    intent = _intent(conn, intent_id)
    _ready(conn, intent, as_of)
    if intent["kind"] == "replacement":
        _replacement_policy(conn, client, intent)
        from backend.replacement_tool import propose_replacement

        remote_id = propose_replacement(
            conn,
            client,
            obligation_id=int(intent["obligation_id"]),
            incident_ref=str(intent["incident_id"]),
        )
        return ExecuteResult("replacement", remote_id, "sent")
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
            )
        except FxQuoteError as error:
            raise ExecuteError(error.code) from error
        if decision.outcome != "converted" or not decision.conversion_id:
            raise ExecuteError(decision.outcome)
        return ExecuteResult("fx_conversion", decision.conversion_id, "converted")
    if intent["kind"] == "beneficiary":
        claim(
            conn,
            kind="beneficiary",
            amount=intent["amount"],
            currency=intent["currency"],
            beneficiary_id=intent["beneficiary_id"],
            details_hash=intent["details_hash"],
            as_of=as_of,
        )
        remote_id = _create_beneficiary(conn, client, intent)
        return ExecuteResult("beneficiary", remote_id, "created")
    if intent["kind"] == "cancel":
        from backend.stop_duplicate import stop_duplicate

        stopped = stop_duplicate(
            conn,
            client,
            approval_id=int(intent["approval_id"]),
            transfer_record_id=int(intent["target_id"]),
        )
        if not stopped.cancelled:
            raise ExecuteError("status_" + stopped.status)
        return ExecuteResult("cancel", str(intent["target_id"]), stopped.status)
    raise ExecuteError("kind")


def claim(conn, *, kind: str, amount: str, currency: str, beneficiary_id: str, details_hash: str, quote_id: str | None = None, target_id: str | None = None, as_of: str | None = None) -> int:
    """Atomically take one matching approved intent. Raises ExecuteError if none is payable."""
    blocked = False
    expired = False
    rows = conn.execute(
        """
        SELECT * FROM execution_intent
        WHERE kind = ? AND state = 'proposed'
          AND amount = ? AND currency = ? AND beneficiary_id = ? AND details_hash = ?
          AND COALESCE(quote_id, '') = ? AND COALESCE(target_id, '') = ?
        ORDER BY id
        """,
        (
            kind,
            format(Decimal(amount), "f"),
            currency,
            beneficiary_id,
            details_hash,
            quote_id or "",
            target_id or "",
        ),
    ).fetchall()
    for intent in rows:
        try:
            _ready(conn, intent, as_of)
        except ExecuteError as error:
            expired = expired or error.code == "expired"
            continue
        if kind == "replacement" and not _code_may_pay(intent["failure_code"]):
            blocked = True
            continue
        cursor = conn.execute(
            "UPDATE execution_intent SET state = 'executed' WHERE id = ? AND state = 'proposed'",
            (intent["id"],),
        )
        if cursor.rowcount == 1:
            return int(intent["id"])
    if blocked:
        raise ExecuteError("policy")
    if expired:
        raise ExecuteError("expired")
    raise ExecuteError("approval")


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
        datetime.fromisoformat(confirmation.attested_at)
    except ValueError:
        raise ExecuteError("callback") from None


def _intent(conn, intent_id: int):
    row = conn.execute("SELECT * FROM execution_intent WHERE id = ?", (intent_id,)).fetchone()
    if row is None:
        raise ExecuteError("missing")
    return row


def intent_terms(conn, intent_id: int) -> ApprovalTerms:
    """The approval terms bound to this intent. P0-2 approves these terms."""
    return _terms(conn, intent_id)


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
    if expires and (as_of is None or as_of >= expires):
        raise ExecuteError("expired")
    if binding_hash(_terms(conn, int(intent["id"]))) != approval["binding_hash"]:
        raise ExecuteError("void")


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


def _create_beneficiary(conn, client, intent) -> str:
    wrapped = json.loads(intent["body_json"] or "{}")
    body = wrapped["body"]
    created = client.create_beneficiary(body)
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
            created.id,
            intent["details_hash"],
            wrapped["bank_name"],
            wrapped["account_last4"],
            wrapped["attested_by"],
            intent["created_at"],
            intent["obligation_id"],
        ),
    )
    conn.commit()
    return created.id

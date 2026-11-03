"""Status-gated proof, a labelled ETA, and a TRACE draft. Nothing is sent."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from backend.ledger import append_audit
from backend.policy import code_from_reason, decide

ETA_LABEL = "Estimated · from Airwallex published flight times"
CUTOFF_LABEL = "Assumed cut-off · not an Airwallex API field"
TRACE_LABEL = "Draft support request · not retrieved from the bank network"

# Business-day spans from Airwallex's published payout flight times. Not an API field.
_FLIGHT_DAYS = {
    ("EUR", "SWIFT"): (1, 3),
    ("GBP", "LOCAL"): (0, 1),
    ("USD", "LOCAL"): (0, 2),
}


@dataclass(frozen=True)
class ProofResult:
    attached: bool
    rule_id: str
    sha256: str | None
    eta: str
    cutoff_label: str | None
    trace_draft: str | None
    reply_draft: str


def issue_proof(conn, client, transfer_record_id: int, *, incident_id: int, seen_at: str) -> ProofResult:
    """Attach a NO_FEE_DISPLAY notice only while the live status is SENT or PAID."""
    record = conn.execute("SELECT * FROM transfer_record WHERE id = ?", (transfer_record_id,)).fetchone()
    remote = client.get_transfer(record["airwallex_id"])
    decision = decide(
        remote.status,
        failure_code=code_from_reason(remote.failure_reason),
        claim="proof",
    )
    eta = describe_eta(remote.transfer_currency or record["currency"], remote.transfer_method)
    cutoff = cutoff_label(remote.transfer_date, remote.dispatch_date)
    if not decision.proof_allowed:
        append_audit(
            conn,
            incident_id=incident_id,
            kind="proof_refused",
            detail=f"status={remote.status}",
            created_at=seen_at,
        )
        conn.commit()
        return ProofResult(
            attached=False,
            rule_id=decision.rule_id,
            sha256=None,
            eta=eta,
            cutoff_label=cutoff,
            trace_draft=None,
            reply_draft=_reply(attached=False, status=remote.status, eta=eta),
        )
    letter = client.create_confirmation_letter(
        transaction_id=remote.id,
        letter_format="NO_FEE_DISPLAY",
    )
    digest = hashlib.sha256(letter.content).hexdigest()
    conn.execute(
        """
        INSERT INTO proof_document (transfer_record_id, format, status_at_issue, sha256, sent_at)
        VALUES (?, 'NO_FEE_DISPLAY', ?, ?, NULL)
        """,
        (transfer_record_id, remote.status, digest),
    )
    append_audit(
        conn,
        incident_id=incident_id,
        kind="proof_attached",
        detail=f"sha256={digest}",
        created_at=seen_at,
    )
    conn.commit()
    trace = trace_draft(remote.short_reference_id or remote.id)
    return ProofResult(
        attached=True,
        rule_id=decision.rule_id,
        sha256=digest,
        eta=eta,
        cutoff_label=cutoff,
        trace_draft=trace if remote.status == "SENT" else None,
        reply_draft=_reply(attached=True, status=remote.status, eta=eta),
    )


def describe_eta(currency: str | None, method: str | None) -> str:
    low, high = _FLIGHT_DAYS.get(((currency or "").upper(), (method or "").upper()), (1, 5))
    return f"{ETA_LABEL}: {low}–{high} business days"


def cutoff_label(transfer_date: str | None, dispatch_date: str | None) -> str | None:
    if transfer_date and dispatch_date and dispatch_date > transfer_date:
        return CUTOFF_LABEL
    return None


def trace_draft(short_reference_id: str) -> str:
    return f"{TRACE_LABEL}. Please send the MT103/UETR advice for {short_reference_id}."


def _reply(*, attached: bool, status: str, eta: str) -> str:
    if not attached and status == "CANCELLED":
        return "We cannot attach the transfer notice. The transfer is cancelled."
    if attached:
        return f"The transfer notice is attached for your records. {eta}"
    return f"The payment is still on its way. {eta}"

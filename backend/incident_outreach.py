"""Non-money execute() handler: supplier replies, TRACE drafts and escalations.

Nothing is sent. An approved intent only queues a message in the outbox and writes an audit
row. The recipient and the exact body are bound into the approval (details_hash), and the
live transfer status is re-read at execute time, so a stale or changed message is refused.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

from backend.ledger import append_audit
from backend.outbox import draft_reply
from backend.policy import Action, code_from_reason, decide
from backend.proof import TRACE_LABEL

ACTIONS = frozenset({"REPLY_WAIT", "REPLY_PROOF", "REPLY_CANCELLED", "TRACE_DRAFT", "ESCALATE"})
# Assumed: the plan says "SENT long enough" with no number. A trace is only drafted at or above this.
TRACE_MIN_AGE_DAYS = 7


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def propose_outreach(conn, *, incident_id: int, obligation_id: int, supplier_id: int,
                     transfer_record_id: int, action: str, body: str, currency: str,
                     proposer: str, expires_at: str, created_at: str):
    """Persist the intent and its approval proposal. Sends nothing."""
    from backend.execute import open_intent

    return open_intent(
        conn, kind="outreach", incident_id=incident_id, obligation_id=obligation_id,
        action=action, amount="0", currency=currency, beneficiary_id=f"supplier:{supplier_id}",
        details_hash=body_hash(body), target_id=str(transfer_record_id),
        body_json=json.dumps({"body": body}), proposer=proposer,
        expires_at=expires_at, created_at=created_at,
    )


def run_outreach(conn, client, intent, as_of: str) -> tuple[str, str]:
    """Gate, claim the exact intent, then queue the message once. Returns (remote_id, status)."""
    from backend.execute import ExecuteError, claim, remember_remote

    action = intent["action"]
    if action not in ACTIONS:
        raise ExecuteError("action")
    try:
        body = json.loads(intent["body_json"] or "")["body"]
    except (ValueError, KeyError, TypeError):
        raise ExecuteError("body") from None
    if not isinstance(body, str) or not body.strip() or body_hash(body) != intent["details_hash"]:
        raise ExecuteError("void")
    done = conn.execute(
        "SELECT id FROM outbox_message WHERE approval_id = ?", (intent["approval_id"],)
    ).fetchone()
    if done is None:
        if intent["state"] != "executed":  # a resumed intent was already gated and claimed
            _gate(conn, client, intent, action, body, as_of)
            claim(
                conn, intent_id=int(intent["id"]), approval_id=int(intent["approval_id"]),
                obligation_id=int(intent["obligation_id"]), as_of=as_of, kind="outreach",
                amount=intent["amount"], currency=intent["currency"],
                beneficiary_id=intent["beneficiary_id"], details_hash=intent["details_hash"],
                target_id=intent["target_id"],
            )
        outbox_id = draft_reply(
            conn, incident_id=int(intent["incident_id"]), approval_id=int(intent["approval_id"]),
            body=body, created_at=as_of,
        )
        kind = {"ESCALATE": "incident_escalated", "TRACE_DRAFT": "trace_queued"}.get(action, "reply_queued")
        append_audit(conn, incident_id=int(intent["incident_id"]), kind=kind,
                     detail=f"intent={intent['id']} action={action} outbox={outbox_id}", created_at=as_of)
    else:
        outbox_id = int(done["id"])
    remember_remote(conn, int(intent["id"]), f"outbox:{outbox_id}", "queued")
    conn.commit()
    return f"outbox:{outbox_id}", "queued"


def _gate(conn, client, intent, action: str, body: str, as_of: str) -> None:
    from backend.execute import ExecuteError

    if action == "ESCALATE":
        if not body.startswith("[ESCALATION R-"):
            raise ExecuteError("body")
        return
    record = conn.execute(
        "SELECT * FROM transfer_record WHERE id = ?", (intent["target_id"],)
    ).fetchone()
    if record is None or record["obligation_id"] != intent["obligation_id"]:
        raise ExecuteError("target")
    remote = client.get_transfer(record["airwallex_id"])  # live status, never the stored one
    code = code_from_reason(remote.failure_reason)
    if action == "REPLY_WAIT":
        if decide(remote.status, failure_code=code, claim="where").action is not Action.WAIT:
            raise ExecuteError("status_gate")
    elif action == "REPLY_PROOF":
        allowed = decide(remote.status, failure_code=code, claim="proof").proof_allowed
        issued = conn.execute(
            "SELECT 1 FROM proof_document WHERE transfer_record_id = ? AND status_at_issue = ?",
            (record["id"], remote.status),
        ).fetchone()
        if not allowed or issued is None:
            raise ExecuteError("proof_gate")
    elif action == "REPLY_CANCELLED":
        if remote.status != "CANCELLED" or decide(remote.status, failure_code=code, claim="proof").proof_allowed:
            raise ExecuteError("status_gate")
    elif action == "TRACE_DRAFT":
        if decide(remote.status, failure_code=code, claim="trace").action is not Action.TRACE:
            raise ExecuteError("status_gate")
        if not body.startswith(TRACE_LABEL) or _age_days(remote.transfer_date, as_of) < TRACE_MIN_AGE_DAYS:
            raise ExecuteError("trace_gate")  # unknown or young age fails closed


def _age_days(transfer_date: str | None, as_of: str) -> int:
    if not transfer_date:
        return -1
    try:
        start = date.fromisoformat(transfer_date[:10])
        now = datetime.fromisoformat(as_of).astimezone(UTC).date()
    except ValueError:
        return -1
    return (now - start).days

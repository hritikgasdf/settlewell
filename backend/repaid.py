"""After a return, a colleague's payment for the same invoice is not replaced."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from backend.ledger import (
    append_audit,
    attach_live_transfer,
    insert_external_transfer,
    set_obligation_status,
)
from backend.replace_payment import ReplaceError

_LIVE = frozenset({"SCHEDULED", "PROCESSING", "SENT", "PAID", "OVERDUE"})


@dataclass(frozen=True)
class RepaidDecision:
    action: str
    airwallex_id: str | None = None


def colleague_repaid(conn, client, *, obligation_id: int, incident_ref: str) -> RepaidDecision:
    """Close against an untagged repayment, or offer a €0 cancel when it is still scheduled."""
    incident_id = _incident_id(incident_ref)
    incident = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if incident is None or obligation is None or incident["obligation_id"] != obligation_id:
        raise ReplaceError("incident")
    if incident["state"] == "CLOSED":
        audit = conn.execute(
            "SELECT detail FROM audit_event WHERE incident_id = ? AND kind = 'colleague_repaid'",
            (incident_id,),
        ).fetchone()
        return RepaidDecision("close", audit["detail"] if audit else None)
    if obligation["live_transfer_id"] is None:
        return RepaidDecision("replace")
    original = conn.execute(
        "SELECT * FROM transfer_record WHERE id = ?",
        (obligation["live_transfer_id"],),
    ).fetchone()
    if original is None or original["status"] != "CANCELLED":
        return RepaidDecision("replace")
    matches = _matches(conn, client, obligation, original)
    if not matches:
        return RepaidDecision("replace")
    scheduled = [item for item in matches if item.status == "SCHEDULED"]
    covered = [item for item in matches if item.status != "SCHEDULED"]
    if scheduled and covered:
        for item in matches:
            _store(conn, obligation, item)
        _audit_once(conn, incident_id, "colleague_cancel", scheduled[0].id)
        conn.commit()
        return RepaidDecision("cancel", scheduled[0].id)
    chosen = covered[0] if covered else scheduled[0]
    local_id = _store(conn, obligation, chosen)
    attach_live_transfer(conn, obligation_id, local_id)
    if chosen.status == "PAID":
        set_obligation_status(conn, obligation_id, "PAID_ONCE")
    conn.execute("UPDATE incident SET state = 'CLOSED' WHERE id = ?", (incident_id,))
    _audit_once(conn, incident_id, "colleague_repaid", chosen.id)
    conn.commit()
    return RepaidDecision("close", chosen.id)


def _incident_id(incident_ref: str) -> int:
    try:
        return int(incident_ref)
    except (TypeError, ValueError):
        raise ReplaceError("incident") from None


def _matches(conn, client, obligation, original) -> list:
    target = Decimal(obligation["amount"])
    found = []
    page_token = None
    for _ in range(5):
        page = client.list_transfers(page_size=50, page=page_token)
        for remote in page.items:
            if not _same_invoice(remote, obligation, target):
                continue
            if remote.id == original["airwallex_id"] or remote.request_id == original["request_id"]:
                continue
            if remote.status not in _LIVE:
                continue
            if _ours(conn, remote.id):
                continue
            found.append(remote)
        if not page.page_after:
            break
        page_token = page.page_after
    return found


def _same_invoice(remote, obligation, target: Decimal) -> bool:
    if remote.reference != obligation["invoice_ref"]:
        return False
    if remote.transfer_currency != obligation["currency"]:
        return False
    return remote.transfer_amount is not None and remote.transfer_amount == target


def _ours(conn, airwallex_id: str) -> bool:
    row = conn.execute(
        "SELECT origin FROM transfer_record WHERE airwallex_id = ?",
        (airwallex_id,),
    ).fetchone()
    return row is not None and row["origin"] == "settlewell"


def _store(conn, obligation, remote) -> int:
    local_id = insert_external_transfer(
        conn,
        airwallex_id=remote.id,
        request_id=remote.request_id,
        obligation_id=obligation["id"],
        amount=format(remote.transfer_amount, "f"),
        currency=remote.transfer_currency or obligation["currency"],
        status=remote.status,
        reference=remote.reference or obligation["invoice_ref"],
    )
    if local_id is not None:
        return local_id
    row = conn.execute(
        "SELECT id FROM transfer_record WHERE airwallex_id = ?",
        (remote.id,),
    ).fetchone()
    return int(row["id"])


def _audit_once(conn, incident_id: int, kind: str, detail: str) -> None:
    existing = conn.execute(
        "SELECT id FROM audit_event WHERE incident_id = ? AND kind = ? AND detail = ?",
        (incident_id, kind, detail),
    ).fetchone()
    if existing is not None:
        return
    append_audit(
        conn,
        incident_id=incident_id,
        kind=kind,
        detail=detail,
        created_at=datetime.now(UTC).isoformat(),
    )

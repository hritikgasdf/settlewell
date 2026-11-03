"""Cancel a scheduled duplicate only after an approval. A refusal is re-read."""

from __future__ import annotations

from dataclasses import dataclass

from backend.airwallex.client import AirwallexError
from backend.approvals import ApprovalTerms, binding_hash
from backend.ledger import set_transfer_status


class StopDuplicateError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StopResult:
    cancelled: bool
    status: str
    reread: bool


def stop_duplicate(conn, client, *, approval_id: int, transfer_record_id: int) -> StopResult:
    """Cancel one scheduled duplicate. Does not post a fee or a balance movement."""
    approval = conn.execute("SELECT * FROM approval WHERE id = ?", (approval_id,)).fetchone()
    record = conn.execute("SELECT * FROM transfer_record WHERE id = ?", (transfer_record_id,)).fetchone()
    if approval is None or record is None:
        raise StopDuplicateError("missing")
    if approval["used_at"] is None or approval["action"] != "STOP_DUPLICATE":
        raise StopDuplicateError("not_approved")
    remote = client.get_transfer(record["airwallex_id"])
    terms = _terms(record, remote.beneficiary_id)
    if binding_hash(terms) != approval["binding_hash"]:
        raise StopDuplicateError("void")
    if remote.status != "SCHEDULED":
        _remember(conn, transfer_record_id, remote)
        return StopResult(False, remote.status, True)
    try:
        client.cancel_transfer(remote.id)
    except AirwallexError as error:
        if error.code != "update_unsupported":
            raise
        again = client.get_transfer(remote.id)
        _remember(conn, transfer_record_id, again)
        return StopResult(False, again.status, True)
    again = client.get_transfer(remote.id)
    _remember(conn, transfer_record_id, again)
    return StopResult(again.status == "CANCELLED", again.status, False)


def _terms(record, beneficiary_id: str | None) -> ApprovalTerms:
    return ApprovalTerms(
        action="STOP_DUPLICATE",
        amount=str(record["amount"]),
        currency=str(record["currency"]),
        beneficiary_id=beneficiary_id or "",
        details_hash=record["details_hash"] or "",
    )


def _remember(conn, transfer_record_id: int, remote) -> None:
    set_transfer_status(
        conn,
        transfer_record_id,
        status=remote.status,
        dispatch_date=remote.dispatch_date,
    )
    conn.commit()

"""Cancel a scheduled duplicate only after an approval. A refusal is re-read."""

from __future__ import annotations

from dataclasses import dataclass

from backend.airwallex.client import AirwallexError
from backend.approvals import binding_hash
from backend.execute import intent_terms
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


def stop_duplicate(
    conn,
    client,
    *,
    approval_id: int,
    transfer_record_id: int,
    as_of: str,
) -> StopResult:
    """Cancel one scheduled duplicate. Does not post a fee or a balance movement."""
    approval = conn.execute("SELECT * FROM approval WHERE id = ?", (approval_id,)).fetchone()
    record = conn.execute("SELECT * FROM transfer_record WHERE id = ?", (transfer_record_id,)).fetchone()
    intent = conn.execute(
        "SELECT * FROM execution_intent WHERE approval_id = ? AND kind = 'cancel'",
        (approval_id,),
    ).fetchone()
    if approval is None or record is None or intent is None:
        raise StopDuplicateError("missing")
    if approval["used_at"] is None or approval["action"] != "STOP_DUPLICATE":
        raise StopDuplicateError("not_approved")
    remote = client.get_transfer(record["airwallex_id"])
    if binding_hash(intent_terms(conn, int(intent["id"]))) != approval["binding_hash"]:
        raise StopDuplicateError("void")
    if (remote.beneficiary_id or "") != intent["beneficiary_id"]:
        raise StopDuplicateError("void")
    if str(record["amount"]) != intent["amount"] or str(record["currency"]) != intent["currency"]:
        raise StopDuplicateError("void")
    if intent["remote_id"]:
        return StopResult(intent["remote_status"] == "CANCELLED", intent["remote_status"] or remote.status, False)
    if intent["state"] == "executed" and remote.status == "CANCELLED":
        from backend.execute import remember_remote

        remember_remote(conn, int(intent["id"]), remote.id, remote.status)
        _remember(conn, transfer_record_id, remote)
        return StopResult(True, remote.status, False)
    if remote.status != "SCHEDULED":
        _remember(conn, transfer_record_id, remote)
        return StopResult(False, remote.status, True)
    from backend.execute import ExecuteError, claim, remember_remote

    try:
        claimed = claim(
            conn,
            intent_id=int(intent["id"]),
            approval_id=approval_id,
            obligation_id=int(intent["obligation_id"]),
            as_of=as_of,
            kind="cancel",
            amount=intent["amount"],
            currency=intent["currency"],
            beneficiary_id=intent["beneficiary_id"],
            details_hash=intent["details_hash"],
            quote_id=intent["quote_id"],
            target_id=str(transfer_record_id),
            request_id=remote.id,
        )
    except ExecuteError as error:
        raise StopDuplicateError(error.code) from error
    conn.commit()
    if claimed.remote_id:
        return StopResult(claimed.remote_status == "CANCELLED", claimed.remote_status or remote.status, False)
    if not claimed.fresh:
        again = client.get_transfer(remote.id)
        if again.status == "CANCELLED":
            remember_remote(conn, int(intent["id"]), again.id, again.status)
            _remember(conn, transfer_record_id, again)
            return StopResult(True, again.status, False)
        if again.status != "SCHEDULED":
            _remember(conn, transfer_record_id, again)
            return StopResult(False, again.status, True)
        raise StopDuplicateError("in_flight")
    try:
        client.cancel_transfer(remote.id)
    except AirwallexError as error:
        if error.code != "update_unsupported":
            raise
        again = client.get_transfer(remote.id)
        _remember(conn, transfer_record_id, again)
        return StopResult(False, again.status, True)
    again = client.get_transfer(remote.id)
    remember_remote(conn, int(intent["id"]), again.id, again.status)
    _remember(conn, transfer_record_id, again)
    return StopResult(again.status == "CANCELLED", again.status, False)


def _remember(conn, transfer_record_id: int, remote) -> None:
    set_transfer_status(
        conn,
        transfer_record_id,
        status=remote.status,
        dispatch_date=remote.dispatch_date,
    )
    conn.commit()

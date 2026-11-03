"""Account-wide scan: a second live payment for one invoice is its own incident."""

from __future__ import annotations

from decimal import Decimal

from backend.ledger import insert_external_transfer, open_duplicate_incident

_LIVE = frozenset({"SCHEDULED", "PROCESSING", "SENT", "PAID", "OVERDUE"})


def scan_paid_once(conn, client, obligation_id: int) -> list[int]:
    """Flag every other live transfer with the same supplier, amount, and reference."""
    obligation = conn.execute(
        "SELECT id, invoice_ref, amount, currency, supplier_id, live_transfer_id FROM obligation WHERE id = ?",
        (obligation_id,),
    ).fetchone()
    live = None
    if obligation["live_transfer_id"] is not None:
        live = conn.execute(
            "SELECT airwallex_id, request_id FROM transfer_record WHERE id = ?",
            (obligation["live_transfer_id"],),
        ).fetchone()
    opened: list[int] = []
    page_token = None
    target = Decimal(obligation["amount"])
    for _ in range(5):
        page = client.list_transfers(page_size=50, page=page_token)
        for remote in page.items:
            if not _is_extra_live(remote, obligation, live, target):
                continue
            insert_external_transfer(
                conn,
                airwallex_id=remote.id,
                request_id=remote.request_id,
                obligation_id=obligation["id"],
                amount=format(remote.transfer_amount, "f"),
                currency=remote.transfer_currency or obligation["currency"],
                status=remote.status,
                reference=remote.reference or obligation["invoice_ref"],
            )
            incident_id = open_duplicate_incident(
                conn,
                obligation_id=obligation["id"],
                airwallex_id=remote.id,
            )
            if incident_id is not None:
                opened.append(incident_id)
        if not page.page_after:
            break
        page_token = page.page_after
    conn.commit()
    return opened


def _is_extra_live(remote, obligation, live, target: Decimal) -> bool:
    if remote.status not in _LIVE:
        return False
    if remote.reference != obligation["invoice_ref"]:
        return False
    if remote.transfer_currency != obligation["currency"]:
        return False
    if remote.transfer_amount is None or remote.transfer_amount != target:
        return False
    if live is None:
        return True
    same_payment = remote.id == live["airwallex_id"] or remote.request_id == live["request_id"]
    return not same_payment

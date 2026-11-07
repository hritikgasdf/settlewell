"""Account-wide scan: a second live payment for one invoice is its own incident."""

from __future__ import annotations

from decimal import Decimal

from backend.ledger import insert_external_transfer, open_duplicate_incident

_LIVE = frozenset({"SCHEDULED", "PROCESSING", "SENT", "PAID", "OVERDUE"})


def scan_paid_once(conn, client, obligation_id: int) -> list[int]:
    """Read every transfer page. Match this supplier's beneficiary versions and invoice."""
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
    versions = _beneficiary_ids(conn, obligation["supplier_id"])
    opened: list[int] = []
    page_token = None
    seen_pages: set[str] = set()
    target = Decimal(obligation["amount"])
    while True:
        page = client.list_transfers(page_size=50, page=page_token)
        for remote in page.items:
            kind = _classify(remote, obligation, live, target, versions)
            if kind is None:
                continue
            insert_external_transfer(
                conn,
                airwallex_id=remote.id,
                request_id=remote.request_id,
                obligation_id=obligation["id"],
                amount=format(remote.transfer_amount, "f") if remote.transfer_amount is not None else obligation["amount"],
                currency=remote.transfer_currency or obligation["currency"],
                status=remote.status,
                reference=remote.reference or obligation["invoice_ref"],
            )
            if kind == "duplicate":
                incident_id = open_duplicate_incident(
                    conn,
                    obligation_id=obligation["id"],
                    airwallex_id=remote.id,
                )
            else:
                incident_id = _open_ambiguous(conn, obligation["id"], remote.id)
            if incident_id is not None:
                opened.append(incident_id)
        if not page.page_after or page.page_after in seen_pages:
            break
        seen_pages.add(page.page_after)
        page_token = page.page_after
    conn.commit()
    return opened


def _beneficiary_ids(conn, supplier_id: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT beneficiary_id FROM bank_detail_version
        WHERE supplier_id = ? AND beneficiary_id IS NOT NULL AND beneficiary_id != ''
        """,
        (supplier_id,),
    ).fetchall()
    return {row["beneficiary_id"] for row in rows}


def _classify(remote, obligation, live, target: Decimal, versions: set[str]) -> str | None:
    """duplicate when supplier version, invoice, amount, and currency all match. Else ambiguous."""
    if remote.status not in _LIVE:
        return None
    if live is not None and (
        remote.id == live["airwallex_id"] or remote.request_id == live["request_id"]
    ):
        return None
    if remote.reference != obligation["invoice_ref"]:
        return None
    owned = bool(remote.beneficiary_id) and remote.beneficiary_id in versions
    same_money = (
        remote.transfer_currency == obligation["currency"]
        and remote.transfer_amount is not None
        and remote.transfer_amount == target
    )
    if owned and same_money:
        return "duplicate"
    return "ambiguous"


def _open_ambiguous(conn, obligation_id: int, airwallex_id: str) -> int | None:
    marker = f"ambiguous:{airwallex_id}"
    existing = conn.execute(
        "SELECT id FROM incident WHERE obligation_id = ? AND claim_json = ?",
        (obligation_id, marker),
    ).fetchone()
    if existing:
        return None
    cursor = conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, ?, 'AMBIGUOUS')",
        (obligation_id, marker),
    )
    return int(cursor.lastrowid)

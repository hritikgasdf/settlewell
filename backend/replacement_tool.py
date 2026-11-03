"""The only replacement tool. Callers pass an obligation and an incident, never an amount."""

from __future__ import annotations

from decimal import Decimal

from backend.replace_payment import ReplaceError, replace_payment, replacement_request_id


def propose_replacement(conn, client, *, obligation_id: int, incident_ref: str) -> str:
    incident = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_ref,)).fetchone()
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if incident is None or obligation is None or incident["obligation_id"] != obligation_id:
        raise ReplaceError("incident")
    original = conn.execute(
        "SELECT * FROM transfer_record WHERE id = ?",
        (obligation["live_transfer_id"],),
    ).fetchone()
    if original is None or not original["airwallex_id"]:
        raise ReplaceError("original")
    remote = client.get_transfer(original["airwallex_id"])
    if remote.transfer_amount is None or not remote.transfer_currency:
        raise ReplaceError("record")
    version = conn.execute(
        """
        SELECT beneficiary_id, details_hash FROM bank_detail_version
        WHERE supplier_id = ? AND source = 'verified_callback' AND beneficiary_id IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (obligation["supplier_id"],),
    ).fetchone()
    if version is None:
        raise ReplaceError("unverified")
    return replace_payment(
        conn,
        client,
        obligation_id=obligation_id,
        attempt=_next_attempt(conn, obligation["invoice_ref"]),
        beneficiary_id=version["beneficiary_id"],
        details_hash=version["details_hash"],
        transfer_method=remote.transfer_method or "SWIFT",
        reason="other_services",
        transfer_amount=Decimal(remote.transfer_amount),
        transfer_currency=remote.transfer_currency,
    )


def _next_attempt(conn, invoice_ref: str) -> int:
    prefix = replacement_request_id(invoice_ref, 1).rsplit("-", 1)[0] + "-"
    rows = conn.execute(
        "SELECT request_id FROM transfer_record WHERE request_id LIKE ?",
        (prefix + "%",),
    ).fetchall()
    numbers = []
    for row in rows:
        suffix = row["request_id"][len(prefix) :]
        if suffix.isdigit():
            numbers.append(int(suffix))
    return max(numbers, default=0) + 1

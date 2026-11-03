"""Build an incident timeline from transfer reads. No webhooks."""

from __future__ import annotations

from decimal import Decimal

from backend.airwallex.types import Transfer
from backend.ledger import (
    append_audit,
    append_status_snapshot,
    hero_bundle,
    insert_external_transfer,
    remember_failure_reason,
    set_obligation_status,
    set_transfer_status,
)

_CLOSED = frozenset({"FAILED", "CANCELLED"})


def poll_incident(conn, client, incident_id: int, *, seen_at: str) -> list[dict]:
    bundle = hero_bundle(conn, incident_id)
    obligation = bundle["obligation"]
    ours = [row for row in bundle["transfers"] if row["origin"] == "settlewell" and row["airwallex_id"]]
    known_ids = {row["airwallex_id"] for row in bundle["transfers"] if row["airwallex_id"]}

    for record in ours:
        remote = client.get_transfer(record["airwallex_id"])
        reversal = _reversal_for(client, record["airwallex_id"], remote.transfer_currency or record["currency"])
        _record_status(conn, record, remote, seen_at)
        if reversal is not None:
            _note_reversal(conn, incident_id, reversal, seen_at)
        _apply_paid(conn, obligation["id"], remote_status=remote.status, reversal_seen=reversal is not None)

    _index_lookalikes(conn, client, obligation, known_ids, incident_id=incident_id, seen_at=seen_at)
    conn.commit()
    return timeline(conn, incident_id)


def timeline(conn, incident_id: int) -> list[dict]:
    incident = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (incident_id,)).fetchone()
    snapshots = conn.execute(
        """
        SELECT 'status' AS kind, snapshot.seen_at AS at, snapshot.status AS detail
        FROM status_snapshot AS snapshot
        JOIN transfer_record ON transfer_record.id = snapshot.transfer_record_id
        WHERE transfer_record.obligation_id = ?
        """,
        (incident["obligation_id"],),
    ).fetchall()
    audits = conn.execute(
        "SELECT kind, created_at AS at, detail FROM audit_event WHERE incident_id = ?",
        (incident_id,),
    ).fetchall()
    rows = [dict(row) for row in snapshots] + [dict(row) for row in audits]
    return sorted(rows, key=lambda row: (row["at"], row["kind"]))


def _record_status(conn, record, remote: Transfer, seen_at: str) -> None:
    previous = conn.execute(
        """
        SELECT status, failure_message FROM status_snapshot
        WHERE transfer_record_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (record["id"],),
    ).fetchone()
    reason = remote.failure_reason or None
    changed = previous is None or previous["status"] != remote.status
    if reason and (previous is None or (previous["failure_message"] or "") != reason):
        changed = True
    set_transfer_status(
        conn,
        record["id"],
        status=remote.status,
        dispatch_date=remote.dispatch_date,
    )
    if reason:
        remember_failure_reason(conn, record["id"], reason)
    if not changed:
        return
    stored = conn.execute(
        "SELECT failure_code, failure_message FROM transfer_record WHERE id = ?",
        (record["id"],),
    ).fetchone()
    append_status_snapshot(
        conn,
        transfer_record_id=record["id"],
        status=remote.status,
        failure_code=stored["failure_code"],
        failure_message=stored["failure_message"],
        source="poll",
        seen_at=seen_at,
    )


def _apply_paid(conn, obligation_id: int, *, remote_status: str, reversal_seen: bool) -> None:
    """A GET of PAID can close the obligation. A later failure or reversal wins."""
    current = conn.execute("SELECT status FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if reversal_seen or remote_status in _CLOSED:
        if current["status"] == "PAID_ONCE":
            set_obligation_status(conn, obligation_id, "IN_FLIGHT")
        return
    if remote_status == "PAID":
        set_obligation_status(conn, obligation_id, "PAID_ONCE")


def _note_reversal(conn, incident_id: int, item, seen_at: str) -> None:
    detail = f"PAYOUT_REVERSAL source={item.source} amount={item.amount}"
    if _audit_exists(conn, incident_id, "payout_reversal", detail):
        return
    append_audit(conn, incident_id=incident_id, kind="payout_reversal", detail=detail, created_at=seen_at)


def _index_lookalikes(conn, client, obligation, known_ids: set[str], *, incident_id: int, seen_at: str) -> None:
    page_token = None
    target = Decimal(obligation["amount"])
    for _ in range(5):
        page = client.list_transfers(page_size=50, page=page_token)
        for remote in page.items:
            if remote.id in known_ids or remote.reference != obligation["invoice_ref"]:
                continue
            if remote.transfer_currency != obligation["currency"]:
                continue
            if remote.transfer_amount is None or remote.transfer_amount != target:
                continue
            inserted = insert_external_transfer(
                conn,
                airwallex_id=remote.id,
                request_id=remote.request_id,
                obligation_id=obligation["id"],
                amount=format(remote.transfer_amount, "f"),
                currency=remote.transfer_currency or obligation["currency"],
                status=remote.status,
                reference=remote.reference or obligation["invoice_ref"],
            )
            known_ids.add(remote.id)
            if inserted is None:
                continue
            detail = f"external airwallex_id={remote.id}"
            if not _audit_exists(conn, incident_id, "external_lookalike", detail):
                append_audit(
                    conn,
                    incident_id=incident_id,
                    kind="external_lookalike",
                    detail=detail,
                    created_at=seen_at,
                )
        if not page.page_after:
            return
        page_token = page.page_after


def _reversal_for(client, transfer_id: str, currency: str | None):
    if not currency:
        return None
    page_token = None
    for _ in range(5):
        history = client.balance_history(currency=currency, page_size=10, page=page_token)
        for item in history.items:
            if item.transaction_type == "PAYOUT_REVERSAL" and item.source == transfer_id:
                return item
        if not history.has_more or not history.page_after:
            return None
        page_token = history.page_after
    return None


def _audit_exists(conn, incident_id: int, kind: str, detail: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM audit_event WHERE incident_id = ? AND kind = ? AND detail = ?",
        (incident_id, kind, detail),
    ).fetchone()
    return row is not None

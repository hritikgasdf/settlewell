"""After a return, a colleague's payment for the same invoice is not replaced."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from backend.ledger import (
    append_audit,
    append_status_snapshot,
    attach_live_transfer,
    insert_external_transfer,
    set_obligation_status,
    set_transfer_status,
)
from backend.outcomes import NOT_PAID_STATUSES, original_returned, reduce_outcome
from backend.paid_once import find_matches, scan_paid_once
from backend.reconcile import _LOOKUP_ERRORS, poll_incident
from backend.replace_payment import ReplaceError


@dataclass(frozen=True)
class RepaidDecision:
    action: str
    airwallex_id: str | None = None


def colleague_repaid(
    conn, client, *, obligation_id: int, incident_ref: str, matches=None
) -> RepaidDecision:
    """Close against an untagged repayment, or offer a €0 cancel when it is still scheduled."""
    incident_id = _incident_id(incident_ref)
    incident = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if incident is None or obligation is None or incident["obligation_id"] != obligation_id:
        raise ReplaceError("incident")
    refreshed = False
    if incident["state"] == "CLOSED":
        # A prior closure stands only if the shared complete-set poll still closes it.
        if _closed_by_poll(conn, client, incident_id):
            audit = conn.execute(
                "SELECT detail FROM audit_event WHERE incident_id = ? AND kind = 'colleague_repaid'",
                (incident_id,),
            ).fetchone()
            return RepaidDecision("close", audit["detail"] if audit else None)
        refreshed = True
        obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if obligation["live_transfer_id"] is None:
        return RepaidDecision("hold" if refreshed else "replace")
    original = conn.execute(
        "SELECT * FROM transfer_record WHERE id = ?",
        (obligation["live_transfer_id"],),
    ).fetchone()
    if original is None or original["status"] != "CANCELLED":
        return RepaidDecision("hold" if refreshed else "replace")
    # Every tracked colleague payment is read by id, authoritative over the list scan.
    # All of them are processed before any decision; one final failure never hides a live one.
    held: str | None = None
    known: dict[str, object] = {}
    for tracked_id in _unresolved_tracked(conn, incident_id):
        if tracked_id in terminal_ids(conn, obligation_id):
            continue  # already final locally; a later stale read never revives it
        remote = client.get_transfer(tracked_id)
        returned = original_returned(client, original)
        verdict = reduce_outcome(original_returned=returned, colleague_status=remote.status)
        if verdict == "hold":
            held = held or tracked_id
        elif verdict == "reopen":
            _sync_status(conn, _store(conn, obligation, remote), remote)
            _audit_dispositions(conn, incident_id, original, remote)
            _audit_once(conn, incident_id, "colleague_reopened", f"{tracked_id}:{remote.status}")
            conn.commit()
        else:
            known[remote.id] = remote
    if held is not None:
        return RepaidDecision("hold", held)
    if matches is None:
        matches = find_matches(conn, client, obligation_id)
    dead = terminal_ids(conn, obligation_id)
    matches = [(kind, remote) for kind, remote in matches if remote.id not in dead]
    if not matches and not known:
        extra = open_extra(conn, client, obligation_id)
        return RepaidDecision("hold", extra) if extra else RepaidDecision("replace")
    # Every match is a supplier-aware look-alike from one complete scan.
    # Scheduled or ambiguous matches open their own incident. Ours stays open.
    scheduled = [remote for _kind, remote in matches if remote.status == "SCHEDULED"]
    if scheduled or any(kind != "duplicate" for kind, _remote in matches):
        scan_paid_once(conn, client, obligation_id, matches)
        if scheduled:
            _audit_once(conn, incident_id, "colleague_cancel", scheduled[0].id)
            conn.commit()
            return RepaidDecision("cancel", scheduled[0].id)
        return RepaidDecision("ambiguous")
    covered = {remote.id: remote for _kind, remote in matches}
    covered.update(known)
    if len(covered) != 1:
        scan_paid_once(conn, client, obligation_id, matches)
        return RepaidDecision("ambiguous")
    chosen = next(iter(covered.values()))
    verdict = reduce_outcome(
        original_returned=original_returned(client, original), colleague_status=chosen.status
    )
    if verdict not in ("close", "track"):
        return RepaidDecision("hold", chosen.id)
    local_id = _store(conn, obligation, chosen)
    _sync_status(conn, local_id, chosen)
    if verdict == "track":
        # Not final. Keep the incident open and the original as the linked transfer.
        _audit_once(conn, incident_id, "colleague_tracked", chosen.id)
        conn.commit()
        return RepaidDecision("track", chosen.id)
    conn.commit()
    # The pair verdict alone never closes: the complete local attempt/payment set must agree.
    if not _closed_by_poll(conn, client, incident_id):
        return RepaidDecision("hold", chosen.id)
    attach_live_transfer(conn, obligation_id, local_id)
    set_obligation_status(conn, obligation_id, "PAID_ONCE")
    _audit_dispositions(conn, incident_id, original, chosen)
    _audit_once(conn, incident_id, "colleague_repaid", chosen.id)
    conn.commit()
    return RepaidDecision("close", chosen.id)


def terminal_ids(conn, obligation_id: int) -> set[str]:
    """Airwallex ids stored FAILED/CANCELLED for this obligation. Terminal outranks any later live read."""
    rows = conn.execute(
        "SELECT airwallex_id, status FROM transfer_record WHERE obligation_id = ? AND airwallex_id IS NOT NULL",
        (obligation_id,),
    ).fetchall()
    return {row["airwallex_id"] for row in rows if row["status"] in NOT_PAID_STATUSES}


def _closed_by_poll(conn, client, incident_id: int) -> bool:
    """Shared complete-outcome decision: unresolved attempts and every known row are re-read."""
    poll_incident(conn, client, incident_id, seen_at=datetime.now(UTC).isoformat())
    row = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_id,)).fetchone()
    return row["state"] == "CLOSED"


def open_extra(conn, client, obligation_id: int) -> str | None:
    """A known external payment, even one the listing omits, that is not terminal by GET blocks a replace."""
    pointer = conn.execute("SELECT live_transfer_id FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    rows = conn.execute(
        """
        SELECT id, airwallex_id, status FROM transfer_record
        WHERE obligation_id = ? AND origin = 'external' AND airwallex_id IS NOT NULL
        ORDER BY id
        """,
        (obligation_id,),
    ).fetchall()
    for row in rows:
        if row["id"] == pointer["live_transfer_id"] or row["status"] in NOT_PAID_STATUSES:
            continue  # the original, or a terminal failure that no later status may revive
        try:
            remote = client.get_transfer(row["airwallex_id"])
        except _LOOKUP_ERRORS:
            return row["airwallex_id"]
        if remote.status not in NOT_PAID_STATUSES:
            return row["airwallex_id"]
    return None


def _unresolved_tracked(conn, incident_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT detail FROM audit_event WHERE incident_id = ? AND kind = 'colleague_tracked' ORDER BY id",
        (incident_id,),
    ).fetchall()
    reopened = conn.execute(
        "SELECT detail FROM audit_event WHERE incident_id = ? AND kind = 'colleague_reopened'",
        (incident_id,),
    ).fetchall()
    done = {row["detail"].split(":", 1)[0] for row in reopened}
    return [row["detail"] for row in rows if row["detail"] not in done]


def _sync_status(conn, local_id: int, remote) -> None:
    stored = conn.execute("SELECT status FROM transfer_record WHERE id = ?", (local_id,)).fetchone()
    if stored["status"] == remote.status:
        return
    if stored["status"] in NOT_PAID_STATUSES:
        return  # a terminal failure is final: no stale live status or PAID may overwrite it
    set_transfer_status(conn, local_id, status=remote.status, dispatch_date=None)
    append_status_snapshot(
        conn,
        transfer_record_id=local_id,
        status=remote.status,
        failure_code=None,
        failure_message=None,
        source="poll",
        seen_at=datetime.now(UTC).isoformat(),
    )


def _audit_dispositions(conn, incident_id: int, original, remote) -> None:
    """Record both outcomes explicitly: the returned original and the colleague payment."""
    _audit_once(conn, incident_id, "original_disposition", f"{original['airwallex_id']}:returned")
    _audit_once(conn, incident_id, "replacement_disposition", f"{remote.id}:{remote.status}")


def _incident_id(incident_ref: str) -> int:
    try:
        return int(incident_ref)
    except (TypeError, ValueError):
        raise ReplaceError("incident") from None


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

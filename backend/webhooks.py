"""Webhook fast path. Polling remains the backstop when delivery is missed.

A payload never unlocks money. It is recorded, a durable settle window opens, and after the
window the shared reconciler reads the remote status, which is the source of truth."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.ledger import (
    append_audit,
    append_status_snapshot,
    remember_failure_reason,
    set_obligation_status,
    set_transfer_status,
)

_STALE = timedelta(minutes=5)
_FUTURE = timedelta(seconds=60)
SETTLE_MS = 10_000
MAX_BODY = 64 * 1024
_FAILURES = {"FAILED", "CANCELLED"}
_RANK = {
    "SCHEDULED": 0,
    "PROCESSING": 1,
    "SENT": 2,
    "PAID": 3,
    "FAILED": 4,
    "CANCELLED": 5,
}
WEBHOOK_EVENTS = (
    "payout.transfer.scheduled",
    "payout.transfer.processing",
    "payout.transfer.sent",
    "payout.transfer.paid",
    "payout.transfer.failed",
    "payout.transfer.cancelled",
    "payout.transfer.overdue",
    "payout.transfer.funding.funded",
    "payout.transfer.in_approval",
)


class WebhookError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WebhookResult:
    outcome: str


@dataclass(frozen=True)
class WebhookRegistration:
    webhook_id: str
    secret: str


def register_webhook(client, *, url: str, request_id: str) -> WebhookRegistration:
    """Subscribe the payout events. The secret stays in the returned object."""
    payload = client.post_json(
        "/api/v1/webhooks/create",
        {
            "url": url,
            "version": "2025-11-11",
            "events": list(WEBHOOK_EVENTS),
            "request_id": request_id,
        },
    )
    return WebhookRegistration(webhook_id=str(payload["id"]), secret=str(payload.get("secret") or ""))


def receive_webhook(
    conn,
    *,
    secret: str,
    raw_body: bytes,
    x_timestamp: str,
    x_signature: str,
    now_ms: int,
) -> WebhookResult:
    """Verify the raw body first, bound it, dedupe by event id, then record in event-time order."""
    if not secret.strip():
        raise WebhookError("unconfigured")
    if len(raw_body) > MAX_BODY:
        raise WebhookError("body")
    _verify(secret, raw_body, x_timestamp, x_signature)
    try:
        timestamp = int(x_timestamp)
    except ValueError:
        raise WebhookError("stale") from None
    skew = datetime.fromtimestamp(now_ms / 1000, UTC) - datetime.fromtimestamp(timestamp / 1000, UTC)
    if skew > _STALE or skew < -_FUTURE:
        raise WebhookError("stale")
    try:
        payload = json.loads(raw_body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WebhookError("body") from None
    if not isinstance(payload, dict) or not payload.get("id"):
        raise WebhookError("body")
    event_id = str(payload["id"])
    name = str(payload.get("name") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    try:  # occurrence time is the payload's; the delivery header only proves freshness
        occurred = int(datetime.fromisoformat(str(payload["created_at"])).timestamp() * 1000)
    except (KeyError, ValueError):
        raise WebhookError("body") from None
    try:
        conn.execute(
            """
            INSERT INTO webhook_event (event_id, name, x_timestamp, signature_ok, processed_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (event_id, name, x_timestamp, _iso(occurred)),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return WebhookResult("duplicate")
    outcome = _apply(conn, name, data, occurred, now_ms, event_id)
    conn.commit()
    return WebhookResult(outcome)


def incident_pending(conn, incident_id: int) -> bool:
    """True while a transfer of this incident's obligation has a webhook that is not yet settled."""
    row = conn.execute(
        """SELECT 1 FROM webhook_state s
           JOIN transfer_record t ON t.airwallex_id = s.transfer_airwallex_id
           JOIN incident i ON i.obligation_id = t.obligation_id WHERE i.id = ? LIMIT 1""",
        (incident_id,),
    ).fetchone()
    return row is not None


def settle(conn, client, now_ms: int) -> list[str]:
    """After the window, a remote read decides, through the shared reconciler. A failed read stays pending."""
    from backend.outcomes import HistoryError
    from backend.reconcile import _LOOKUP_ERRORS, poll_incident

    done: list[str] = []
    due = conn.execute(
        "SELECT transfer_airwallex_id, event_id FROM webhook_state WHERE settle_until_ms <= ?", (now_ms,)
    ).fetchall()
    for state in due:
        transfer_id = state["transfer_airwallex_id"]
        row = conn.execute(
            """SELECT i.id AS incident_id FROM transfer_record t JOIN incident i ON i.obligation_id = t.obligation_id
               WHERE t.airwallex_id = ? ORDER BY i.id LIMIT 1""",
            (transfer_id,),
        ).fetchone()
        if row is not None:
            seen = _iso(now_ms)
            report: dict = {}
            try:
                poll_incident(conn, client, row["incident_id"], seen_at=seen, settled=True, report=report)
                if report.get("incomplete", True):
                    raise HistoryError("incomplete remote evidence")
            except _LOOKUP_ERRORS:
                conn.rollback()
                append_audit(conn, incident_id=row["incident_id"], kind="webhook_poll_failed", detail=transfer_id, created_at=seen)
                conn.commit()
                continue  # unknown remote evidence: stay pending, nothing unlocks
        current = conn.execute(
            "SELECT event_id FROM webhook_state WHERE transfer_airwallex_id = ?", (transfer_id,)
        ).fetchone()
        if current is not None and current["event_id"] == state["event_id"]:  # a newer event keeps its window
            conn.execute("DELETE FROM webhook_state WHERE transfer_airwallex_id = ?", (transfer_id,))
            done.append(transfer_id)
        conn.commit()
    return done


def _verify(secret: str, raw_body: bytes, x_timestamp: str, x_signature: str) -> None:
    digest = hmac.new(secret.encode(), x_timestamp.encode() + raw_body, hashlib.sha256).hexdigest()
    try:
        matches = hmac.compare_digest(digest, x_signature.lower())
    except (ValueError, TypeError):
        matches = False
    if not matches:
        raise WebhookError("signature")


def _apply(conn, name: str, data: dict, occurred: int, now_ms: int, event_id: str) -> str:
    """Record the event and open a settle window. Only a newer failure moves state, and only to block."""
    if ".funding." in name:
        return "ignored"
    transfer_id = data.get("id")
    status = str(data.get("status") or "").upper()
    if not transfer_id or not status:
        return "ignored"
    record = conn.execute("SELECT * FROM transfer_record WHERE airwallex_id = ?", (str(transfer_id),)).fetchone()
    if record is None:
        return "ignored"
    reason = data.get("failure_reason")
    if isinstance(reason, str) and reason:
        remember_failure_reason(conn, record["id"], reason)
    state = conn.execute("SELECT * FROM webhook_state WHERE transfer_airwallex_id = ?", (str(transfer_id),)).fetchone()
    newer = state is None or occurred > state["latest_event_ts_ms"]
    tie_worse = (
        state is not None
        and occurred == state["latest_event_ts_ms"]
        and status in _FAILURES
        and state["latest_status"] not in _FAILURES
    )
    if not (newer or tie_worse):
        return "applied" if reason else "ignored"  # older event: no window, no state change, failure kept
    conn.execute(
        """INSERT INTO webhook_state (transfer_airwallex_id, latest_event_ts_ms, latest_status, settle_until_ms, event_id)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT(transfer_airwallex_id) DO UPDATE SET
           latest_event_ts_ms = excluded.latest_event_ts_ms, latest_status = excluded.latest_status,
           settle_until_ms = excluded.settle_until_ms, event_id = excluded.event_id""",
        (str(transfer_id), occurred, status, now_ms + SETTLE_MS, event_id),
    )
    stored = conn.execute(
        "SELECT failure_code, failure_message FROM transfer_record WHERE id = ?", (record["id"],)
    ).fetchone()
    append_status_snapshot(
        conn,
        transfer_record_id=record["id"],
        status=status,
        failure_code=stored["failure_code"],
        failure_message=stored["failure_message"],
        source="webhook",
        seen_at=_iso(occurred),
    )
    if status in _FAILURES:  # blocking direction only; success is never taken from a payload
        set_transfer_status(conn, record["id"], status=status, dispatch_date=data.get("dispatch_date"))
        if record["obligation_id"] is not None:
            current = conn.execute("SELECT status FROM obligation WHERE id = ?", (record["obligation_id"],)).fetchone()
            if current is not None and current["status"] == "PAID_ONCE":
                set_obligation_status(conn, record["obligation_id"], "IN_FLIGHT")
    elif _RANK.get(status, 99) < _RANK["PAID"] and _RANK[status] >= _RANK.get(record["status"] or "", -1):
        set_transfer_status(conn, record["id"], status=status, dispatch_date=data.get("dispatch_date"))
    return "applied"


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat()

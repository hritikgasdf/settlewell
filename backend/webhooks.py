"""Webhook fast path. Polling remains the backstop when delivery is missed."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.ledger import (
    append_status_snapshot,
    remember_failure_reason,
    set_obligation_status,
    set_transfer_status,
)

_STALE = timedelta(minutes=5)
_FUTURE = timedelta(seconds=60)
_SETTLE = timedelta(seconds=10)
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
    """Verify, dedupe, then apply in timestamp and status-rank order."""
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
    try:
        conn.execute(
            """
            INSERT INTO webhook_event (event_id, name, x_timestamp, signature_ok, processed_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (event_id, name, x_timestamp, _iso(timestamp)),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return WebhookResult("duplicate")
    outcome = _apply(conn, name, data, timestamp)
    conn.commit()
    return WebhookResult(outcome)


def promote_paid(conn, now_ms: int) -> None:
    """A webhook PAID becomes paid-once only after the settle window, with no failure after it."""
    now = datetime.fromtimestamp(now_ms / 1000, UTC)
    rows = conn.execute(
        """
        SELECT transfer_record.id AS transfer_id, transfer_record.obligation_id, snapshot.seen_at
        FROM transfer_record
        JOIN status_snapshot AS snapshot ON snapshot.transfer_record_id = transfer_record.id
        WHERE transfer_record.status = 'PAID' AND snapshot.status = 'PAID' AND snapshot.source = 'webhook'
        ORDER BY snapshot.id DESC
        """
    ).fetchall()
    seen: set[int] = set()
    for row in rows:
        if row["transfer_id"] in seen or row["obligation_id"] is None:
            continue
        seen.add(row["transfer_id"])
        paid_at = datetime.fromisoformat(row["seen_at"])
        if now - paid_at < _SETTLE:
            continue
        set_obligation_status(conn, row["obligation_id"], "PAID_ONCE")
    conn.commit()


def _verify(secret: str, raw_body: bytes, x_timestamp: str, x_signature: str) -> None:
    digest = hmac.new(secret.encode(), x_timestamp.encode() + raw_body, hashlib.sha256).hexdigest()
    try:
        matches = hmac.compare_digest(digest, x_signature.lower())
    except ValueError:
        matches = False
    if not matches:
        raise WebhookError("signature")


def _apply(conn, name: str, data: dict, timestamp: int) -> str:
    if ".funding." in name:
        return "ignored"
    transfer_id = data.get("id")
    status = str(data.get("status") or "").upper()
    if not transfer_id or not status:
        return "ignored"
    record = conn.execute(
        "SELECT * FROM transfer_record WHERE airwallex_id = ?",
        (str(transfer_id),),
    ).fetchone()
    if record is None:
        return "ignored"
    reason = data.get("failure_reason")
    if isinstance(reason, str) and reason:
        remember_failure_reason(conn, record["id"], reason)
    if _RANK.get(status, -1) < _RANK.get(record["status"] or "", -1):
        return "applied" if reason else "ignored"
    set_transfer_status(conn, record["id"], status=status, dispatch_date=data.get("dispatch_date"))
    stored = conn.execute(
        "SELECT failure_code, failure_message FROM transfer_record WHERE id = ?",
        (record["id"],),
    ).fetchone()
    append_status_snapshot(
        conn,
        transfer_record_id=record["id"],
        status=status,
        failure_code=stored["failure_code"],
        failure_message=stored["failure_message"],
        source="webhook",
        seen_at=_iso(timestamp),
    )
    if status in {"FAILED", "CANCELLED"} and record["obligation_id"] is not None:
        current = conn.execute(
            "SELECT status FROM obligation WHERE id = ?",
            (record["obligation_id"],),
        ).fetchone()
        if current is not None and current["status"] == "PAID_ONCE":
            set_obligation_status(conn, record["obligation_id"], "IN_FLIGHT")
    return "applied"


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat()

"""Out-of-order and replayed webhooks leave the transfer in the right state. No network."""

import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime

import pytest

from backend.ledger import connect, seed_hero
from backend.webhooks import (
    WebhookError,
    receive_webhook,
    register_webhook,
)

SECRET = "whsec-test"
NOW = 1_700_000_000_000


def _signed(body: dict, timestamp: int, *, secret: str = SECRET) -> tuple[bytes, str]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), str(timestamp).encode() + raw, hashlib.sha256).hexdigest()
    return raw, digest


def _event(event_id: str, name: str, timestamp: int, status: str, **data) -> dict:
    payload = {"id": "awx-hero", "status": status, "transfer_currency": "EUR", "transfer_amount": "4200.00"}
    payload.update(data)
    return {"id": event_id, "name": name, "data": payload, "created_at": datetime.fromtimestamp(timestamp / 1000, UTC).isoformat()}


def _deliver(conn, body: dict, timestamp: int, *, now: int = NOW, secret: str = SECRET):
    raw, signature = _signed(body, timestamp, secret=secret)
    return receive_webhook(
        conn,
        secret=SECRET,
        raw_body=raw,
        x_timestamp=str(timestamp),
        x_signature=signature,
        now_ms=now,
    )


def _hero(conn, status: str | None = "SENT"):
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    if status is None:
        conn.execute("UPDATE transfer_record SET status = NULL WHERE airwallex_id = 'awx-hero'")
    else:
        conn.execute("UPDATE transfer_record SET status = ? WHERE airwallex_id = 'awx-hero'", (status,))
    conn.commit()
    return incident_id


def _status(conn) -> str | None:
    return conn.execute("SELECT status FROM transfer_record WHERE airwallex_id = 'awx-hero'").fetchone()["status"]


def _reason(conn) -> str | None:
    return conn.execute(
        "SELECT failure_message FROM transfer_record WHERE airwallex_id = 'awx-hero'"
    ).fetchone()["failure_message"]


def _obligation(conn) -> str:
    return conn.execute("SELECT status FROM obligation").fetchone()["status"]


def test_a_bad_signature_changes_nothing():
    conn = connect()
    _hero(conn)
    raw, _signature = _signed(_event("evt-1", "payout.transfer.sent", NOW, "SENT"), NOW)

    with pytest.raises(WebhookError) as caught:
        receive_webhook(
            conn,
            secret=SECRET,
            raw_body=raw,
            x_timestamp=str(NOW),
            x_signature="0" * 64,
            now_ms=NOW,
        )

    assert caught.value.code == "signature"
    assert _status(conn) == "SENT"
    assert conn.execute("SELECT COUNT(*) AS n FROM webhook_event").fetchone()["n"] == 0


def test_a_stale_timestamp_is_rejected():
    conn = connect()
    _hero(conn)
    body = _event("evt-old", "payout.transfer.sent", NOW, "SENT")
    raw, signature = _signed(body, NOW - 6 * 60 * 1000)

    with pytest.raises(WebhookError) as caught:
        receive_webhook(
            conn,
            secret=SECRET,
            raw_body=raw,
            x_timestamp=str(NOW - 6 * 60 * 1000),
            x_signature=signature,
            now_ms=NOW,
        )

    assert caught.value.code == "stale"
    assert _status(conn) == "SENT"


def test_processing_before_scheduled_stays_at_the_later_status():
    conn = connect()
    _hero(conn, status=None)
    processing = _event("evt-proc", "payout.transfer.processing", NOW + 500, "PROCESSING")
    scheduled = _event("evt-sched", "payout.transfer.scheduled", NOW, "SCHEDULED")

    _deliver(conn, processing, NOW + 500)
    _deliver(conn, scheduled, NOW)

    assert _status(conn) == "PROCESSING"
    assert conn.execute("SELECT COUNT(*) AS n FROM status_snapshot").fetchone()["n"] == 1


def test_cancelled_before_failed_keeps_the_reason_and_does_not_mark_paid():
    conn = connect()
    _hero(conn, status="SENT")
    paid = _event("evt-paid", "payout.transfer.paid", NOW, "PAID")
    failed = _event(
        "evt-failed",
        "payout.transfer.failed",
        NOW + 10,
        "FAILED",
        failure_reason="90802: Beneficiary bank returned",
    )
    cancelled = _event("evt-cancel", "payout.transfer.cancelled", NOW + 1000, "CANCELLED")

    _deliver(conn, cancelled, NOW + 1000)
    _deliver(conn, failed, NOW + 10)
    _deliver(conn, paid, NOW)

    assert _status(conn) == "CANCELLED"
    assert _reason(conn) == "90802: Beneficiary bank returned"
    assert _obligation(conn) == "OPEN"
    assert _obligation(conn) == "OPEN"


def test_a_replayed_event_does_not_apply_twice():
    conn = connect()
    _hero(conn, status="PROCESSING")
    body = _event("evt-sent", "payout.transfer.sent", NOW, "SENT")

    first = _deliver(conn, body, NOW)
    second = _deliver(conn, body, NOW)

    assert first.outcome == "applied"
    assert second.outcome == "duplicate"
    assert conn.execute("SELECT COUNT(*) AS n FROM status_snapshot").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM webhook_event").fetchone()["n"] == 1


def test_concurrent_replays_apply_the_event_once(tmp_path):
    path = tmp_path / "hooks.db"
    conn = connect(path)
    _hero(conn, status="SENT")
    conn.close()
    body = _event("evt-race", "payout.transfer.paid", NOW, "PAID")
    raw, signature = _signed(body, NOW)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker() -> None:
        local = connect(path)
        barrier.wait()
        result = receive_webhook(
            local,
            secret=SECRET,
            raw_body=raw,
            x_timestamp=str(NOW),
            x_signature=signature,
            now_ms=NOW,
        )
        outcomes.append(result.outcome)
        local.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["applied", "duplicate"]
    stored = connect(path)
    assert _status(stored) == "SENT"  # a payload is recorded, never taken as success
    assert stored.execute("SELECT COUNT(*) FROM webhook_state").fetchone()[0] == 1
    assert stored.execute("SELECT COUNT(*) AS n FROM webhook_event").fetchone()["n"] == 1
    assert stored.execute("SELECT COUNT(*) AS n FROM status_snapshot").fetchone()["n"] == 1


def test_funding_funded_does_not_move_the_transfer():
    conn = connect()
    _hero(conn, status="SENT")
    body = _event("evt-fund", "payout.transfer.funding.funded", NOW, "SENT")

    result = _deliver(conn, body, NOW)

    assert result.outcome == "ignored"
    assert _status(conn) == "SENT"
    assert conn.execute("SELECT COUNT(*) AS n FROM status_snapshot").fetchone()["n"] == 0


class Api:
    def __init__(self) -> None:
        self.path = ""
        self.body: dict = {}

    def post_json(self, path: str, body: dict) -> dict:
        self.path = path
        self.body = body
        return {"id": "wh_1", "secret": "whsec-live"}


def test_register_webhook_subscribes_without_storing_the_secret():
    conn = connect()
    _hero(conn)
    api = Api()

    registration = register_webhook(api, url="https://example.test/hooks", request_id="stl-wh-1")

    assert registration.webhook_id == "wh_1"
    assert api.path == "/api/v1/webhooks/create"
    assert api.body["version"] == "2025-11-11"
    assert api.body["url"] == "https://example.test/hooks"
    assert "payout.transfer.failed" in api.body["events"]
    assert "payout.transfer.funding.funded" in api.body["events"]
    stored = " ".join(str(row) for row in conn.execute("SELECT * FROM webhook_event").fetchall())
    assert "whsec-live" not in stored
    assert registration.secret == "whsec-live"

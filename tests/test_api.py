"""P1-5 API. Offline TestClient; writes must pass the execute() gate and persist."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import api as api_module
from backend import execute as execute_module
from backend.airwallex.client import AirwallexError
from backend.api import create_app

ORIGIN = "http://127.0.0.1:8765"


def _boot(path, monkeypatch=None):
    client = TestClient(create_app(str(path)), base_url=ORIGIN)
    token = client.get("/api/session").json()["csrf_token"]
    return client, {"Origin": ORIGIN, "X-Settlewell-CSRF": token}


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "demo.db"
    client, headers = _boot(path)
    detail = client.get("/api/incidents/1").json()
    intent = detail["proposals"][0]
    base = f"/api/incidents/1/proposals/{intent['id']}"
    return client, headers, path, intent, base


def _rows(path, sql, *args):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _remote(path):
    return _rows(path, "SELECT status FROM mock_remote WHERE airwallex_id='synthetic-dup-2291'")[0][0]


def test_get_is_read_only_and_shows_why(env):
    client, _, path, _, _ = env
    before = _rows(path, "SELECT (SELECT COUNT(*) FROM audit_event) a, (SELECT COUNT(*) FROM approval WHERE used_at IS NOT NULL) u")[0]
    detail = client.get("/api/incidents/1").json()
    client.get("/api/incidents")
    after = _rows(path, "SELECT (SELECT COUNT(*) FROM audit_event) a, (SELECT COUNT(*) FROM approval WHERE used_at IS NOT NULL) u")[0]
    assert tuple(before) == tuple(after) and after["u"] == 0
    assert detail["banner"].startswith("MOCK") and detail["pinned_bank"]["account_last4"] == "4400"
    assert detail["proposals"][0]["can_approve"] is True
    # The seeded demo DB now carries a matched claim and one stored MOCK/SYNTHETIC PDF on the live transfer.
    assert detail["incident"]["evidence"]["claim_matched"] is True
    assert detail["incident"]["claim"]["matched"] is True
    link = "/api/incidents/1/proof/1/pdf"
    assert detail["proof"]["pdf_link"] == link
    doc = detail["proof"]["documents"][0]
    assert doc["pdf_url"] == link and doc["refusal"] is None
    assert doc["origin"] == "mock_synthetic" and "MOCK / SYNTHETIC" in doc["label"]
    assert client.get("/api/incidents").json()["incidents"][0]["pending_proposals"] == 1
    # Fetching the linked PDF is a read: it returns the stored bytes and writes nothing.
    pdf = client.get(link)
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert pdf.headers["content-type"].startswith("application/pdf")
    after_pdf = _rows(path, "SELECT (SELECT COUNT(*) FROM audit_event) a, (SELECT COUNT(*) FROM approval WHERE used_at IS NOT NULL) u")[0]
    assert tuple(after_pdf) == tuple(before)


def test_approve_goes_through_gate_and_cancels_durably(env, monkeypatch):
    client, headers, path, intent, base = env
    calls = []
    real = execute_module.execute
    monkeypatch.setattr(api_module, "execute", lambda *a, **k: (calls.append(a[2]), real(*a, **k))[1])
    response = client.post(f"{base}/approve", json={"binding_hash": intent["binding_hash"]}, headers=headers)
    assert response.status_code == 200, response.text
    assert calls == [intent["id"]]
    assert response.json()["result"]["status"] == "CANCELLED"
    assert _remote(path) == "CANCELLED"
    assert _rows(path, "SELECT status FROM transfer_record WHERE id = ?", intent["target_id"])[0][0] == "CANCELLED"
    row = _rows(path, "SELECT state, remote_status FROM execution_intent WHERE id = ?", intent["id"])[0]
    assert (row["state"], row["remote_status"]) == ("executed", "CANCELLED")
    kinds = [r[0] for r in _rows(path, "SELECT kind FROM audit_event ORDER BY id")]
    assert kinds[-2:] == ["intent_approved", "intent_executed"]


def test_replay_is_refused_and_restart_still_refuses(env):
    client, headers, path, intent, base = env
    body = {"binding_hash": intent["binding_hash"]}
    assert client.post(f"{base}/approve", json=body, headers=headers).status_code == 200
    again = client.post(f"{base}/approve", json=body, headers=headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "approval_used"
    # restart: a new app on the same file; the demo is not reseeded
    client2, headers2 = _boot(path)
    assert len(_rows(path, "SELECT id FROM incident")) == 1
    replay = client2.post(f"{base}/approve", json=body, headers=headers2)
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "approval_used"
    assert client2.post(f"{base}/reject", json={**body, "reason": "x"}, headers=headers2).status_code == 409
    assert _remote(path) == "CANCELLED"


def test_reject_is_terminal_audited_and_never_sends(env):
    client, headers, path, intent, base = env
    body = {"binding_hash": intent["binding_hash"], "reason": "not a duplicate"}
    response = client.post(f"{base}/reject", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["intent"]["state"] == "refused"
    assert _remote(path) == "SCHEDULED"
    assert _rows(path, "SELECT kind FROM audit_event ORDER BY id DESC LIMIT 1")[0][0] == "intent_rejected"
    after = client.post(f"{base}/approve", json={"binding_hash": intent["binding_hash"]}, headers=headers)
    assert after.status_code == 409 and after.json()["error"]["code"] == "intent_refused"
    with pytest.raises(execute_module.ExecuteError) as caught:  # gate itself also refuses
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        execute_module.execute(conn, api_module.LedgerMockClient(conn), intent["id"], as_of="2026-11-08T00:00:00+00:00")
    assert caught.value.code == "refused"
    assert _remote(path) == "SCHEDULED"


def test_cancel_route_opens_intent_and_uses_gate(tmp_path, monkeypatch):
    client, headers = _boot(tmp_path / "c.db")
    reject_first = client.get("/api/incidents/1").json()["proposals"][0]
    client.post(f"/api/incidents/1/proposals/{reject_first['id']}/reject",
                json={"binding_hash": reject_first["binding_hash"], "reason": "redo"}, headers=headers)
    dup = next(t for t in client.get("/api/incidents/1").json()["transfers"] if t["cancellable"])
    stale = client.post(f"/api/incidents/1/transfers/{dup['id']}/cancel", json={"expected_status": "SENT"}, headers=headers)
    assert stale.json()["error"]["code"] == "stale_status"
    ok = client.post(f"/api/incidents/1/transfers/{dup['id']}/cancel", json={"expected_status": "SCHEDULED"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["result"]["status"] == "CANCELLED"
    assert ok.json()["intent"]["approval"]["state"] == "consumed"
    again = client.post(f"/api/incidents/1/transfers/{dup['id']}/cancel", json={"expected_status": "SCHEDULED"}, headers=headers)
    assert again.status_code == 409


def test_sent_payment_is_never_cancelled_and_no_direct_remote_calls(env, monkeypatch):
    client, headers, path, _, _ = env
    live = next(t for t in client.get("/api/incidents/1").json()["transfers"] if t["is_live"])
    refused = client.post(f"/api/incidents/1/transfers/{live['id']}/cancel", json={"expected_status": "SENT"}, headers=headers)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_cancellable"
    assert _rows(path, "SELECT status FROM mock_remote WHERE airwallex_id='synthetic-inv-2291'")[0][0] == "SENT"
    # remote moved to SENT behind our back: the gate re-reads, refuses and accounts for it
    conn = sqlite3.connect(path)
    conn.execute("UPDATE mock_remote SET status='SENT' WHERE airwallex_id='synthetic-dup-2291'")
    conn.commit()
    conn.close()
    intent = client.get("/api/incidents/1").json()["proposals"][0]
    gone = client.post(f"/api/incidents/1/proposals/{intent['id']}/approve", json={"binding_hash": intent["binding_hash"]}, headers=headers)
    assert gone.status_code == 409 and gone.json()["error"]["gate_code"] == "status_SENT"
    assert _rows(path, "SELECT status FROM transfer_record WHERE id = ?", intent["target_id"])[0][0] == "SENT"
    assert _rows(path, "SELECT state FROM execution_intent WHERE id = ?", intent["id"])[0][0] == "refused"
    src = Path(api_module.__file__).read_text()
    assert "cancel_transfer(" not in src.replace("def cancel_transfer(", "") and ".create_transfer" not in src


def test_cross_incident_tampered_and_extra_fields_are_refused(env):
    client, headers, path, intent, base = env
    other = client.post(f"/api/incidents/2/proposals/{intent['id']}/approve", json={"binding_hash": intent["binding_hash"]}, headers=headers)
    assert other.status_code == 409 and other.json()["error"]["code"] == "cross_incident"
    stale = client.post(f"{base}/approve", json={"binding_hash": "0" * 64}, headers=headers)
    assert stale.json()["error"]["code"] == "stale_terms"
    for extra in ({"amount": "1.00"}, {"actor": "ap-clerk"}, {"approval_id": 1}, {"beneficiary_id": "x"}):
        r = client.post(f"{base}/approve", json={"binding_hash": intent["binding_hash"], **extra}, headers=headers)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    conn = sqlite3.connect(path)  # tamper with stored terms: gate voids it
    conn.execute("UPDATE execution_intent SET amount = '1.00' WHERE id = ?", (intent["id"],))
    conn.commit()
    conn.close()
    tampered = client.post(f"{base}/approve", json={"binding_hash": intent["binding_hash"]}, headers=headers)
    assert tampered.status_code == 409 and tampered.json()["error"]["code"] == "stale_terms"
    assert _remote(path) == "SCHEDULED"
    assert _rows(path, "SELECT COUNT(*) FROM approval WHERE used_at IS NOT NULL")[0][0] == 0


def test_missing_or_unsafe_context_fails_closed(env, monkeypatch):
    client, headers, path, intent, base = env
    body = {"binding_hash": intent["binding_hash"]}
    cases = [
        ({}, "origin_missing"),
        ({"Origin": ORIGIN}, "csrf_missing"),
        ({"Origin": "https://evil.example", "X-Settlewell-CSRF": headers["X-Settlewell-CSRF"]}, "origin_forbidden"),
        ({"Origin": ORIGIN, "X-Settlewell-CSRF": "wrong"}, "csrf_invalid"),
    ]
    for hdrs, code in cases:
        r = client.post(f"{base}/approve", json=body, headers=hdrs)
        assert r.status_code == 403 and r.json()["error"]["code"] == code
    bad_host = client.get("/api/incidents", headers={"Host": "attacker.example"})
    assert bad_host.status_code == 403
    monkeypatch.setenv("SETTLEWELL_MOCK", "0")
    off = client.post(f"{base}/approve", json=body, headers=headers)
    assert off.status_code == 503 and off.json()["error"]["code"] == "writes_disabled"
    assert _remote(path) == "SCHEDULED"
    assert _rows(path, "SELECT COUNT(*) FROM approval WHERE used_at IS NOT NULL")[0][0] == 0


def test_remote_failure_fails_closed_without_cancelling(tmp_path):
    class Broken(api_module.LedgerMockClient):
        def cancel_transfer(self, transfer_id):
            raise AirwallexError(400, "update_unsupported", "")

    path = tmp_path / "b.db"
    app = create_app(str(path), client_factory=Broken)
    client = TestClient(app, base_url=ORIGIN)
    headers = {"Origin": ORIGIN, "X-Settlewell-CSRF": client.get("/api/session").json()["csrf_token"]}
    intent = client.get("/api/incidents/1").json()["proposals"][0]
    r = client.post(f"/api/incidents/1/proposals/{intent['id']}/approve", json={"binding_hash": intent["binding_hash"]}, headers=headers)
    assert r.status_code == 409 and _remote(path) == "SCHEDULED"

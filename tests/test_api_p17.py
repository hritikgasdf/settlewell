"""P1-7 API carry-overs: seeded claim, status-gated stored proof PDF, real execute() approvals.

Offline. Replacement and FX tests run the real execute() gate against a persisted DB; only the
remote Airwallex client is a stateful in-memory fake (balances move on create/convert).
"""

import hashlib
import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_execute import SEEN, Pay, _ready
from test_funding_execute import Funded, rows

from backend import api as api_module
from backend import claims
from backend.api import create_app
from backend.api_proof import MOCK_LABEL, build_mock_pdf
from backend.approvals import AP_CLERK
from backend.execute import open_intent
from backend.fx_funding import fund_replacement
from backend.ledger import connect, migrate

ORIGIN = "http://127.0.0.1:8765"
FAR = "2999-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _no_live(*_a, **_k):
        raise AssertionError("live Claude client must not be created")

    monkeypatch.setattr(claims, "_make_client", _no_live)


def _boot(path, **kwargs):
    client = TestClient(create_app(str(path), **kwargs), base_url=ORIGIN)
    token = client.get("/api/session").json()["csrf_token"]
    return client, {"Origin": ORIGIN, "X-Settlewell-CSRF": token}


def _sql(path, sql, *args, write=False):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        out = conn.execute(sql, args).fetchall()
        if write:
            conn.commit()
        return out
    finally:
        conn.close()


def _dump(path):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t}").fetchall()
                for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()


@pytest.fixture
def demo(tmp_path):
    path = tmp_path / "demo.db"
    client, headers = _boot(path)
    return client, headers, path


# ---- 1. seeded claim ----


def test_seeded_claim_is_the_real_offline_parse_with_pinned_identity(demo):
    client, _, path = demo
    inc = client.get("/api/incidents/1").json()
    claim = inc["incident"]["claim"]
    supplier_id = _sql(path, "SELECT id FROM supplier WHERE name = 'Lumen Textiles GmbH'")[0][0]
    assert (claim["invoice_ref"], claim["amount"], claim["currency"]) == ("INV-2291", "4200", "EUR")
    assert (claim["asks_where"], claim["asks_proof"], claim["asks_new_account"]) == (True, False, True)
    assert claim["rule_id"] == "R-MATCHED" and claim["matched"] is True
    assert claim["pinned_supplier_id"] == supplier_id  # from our obligation, not the email
    assert claim["obligation_id"] == inc["obligation"]["id"]
    assert claim["transfer_id"] == inc["obligation"]["live_transfer_id"]
    assert inc["incident"]["evidence"] == {"claim_recorded": True, "claim_matched": True}
    assert "ap@lumen.example" not in str(claim) and "Lumen" not in str(claim)  # nothing email-derived


def test_existing_db_is_not_reseeded_or_rewritten(demo, tmp_path):
    _, _, path = demo
    _sql(path, "UPDATE incident SET claim_json = NULL", write=True)
    _sql(path, "DELETE FROM proof_blob", write=True)
    _sql(path, "DELETE FROM proof_document", write=True)
    before = _dump(path)
    client, _ = _boot(path)  # restart on a populated DB
    assert _dump(path) == before
    inc = client.get("/api/incidents/1").json()
    assert inc["incident"]["claim"] is None and inc["proof"]["pdf_link"] is None


def test_legacy_db_gets_only_the_additive_blob_table(tmp_path):
    path = tmp_path / "legacy.db"
    conn = connect(path)
    migrate(conn)
    _ready(conn)
    conn.close()
    before = {k: v for k, v in _dump(path).items()}
    _boot(path)
    after = _dump(path)
    assert {k: v for k, v in after.items() if k in before} == before
    assert set(after) - set(before) == {"mock_remote"}  # proof_blob is created by ledger.connect


# ---- 2. proof PDF ----


def _link(client):
    return client.get("/api/incidents/1").json()["proof"]


def test_proof_link_and_pdf_bytes_are_the_stored_mock_document(demo):
    client, _, path = demo
    before = _dump(path)
    proof = _link(client)
    assert proof["pdf_link"] == "/api/incidents/1/proof/1/pdf"
    doc = proof["documents"][0]
    assert doc["origin"] == "mock_synthetic" and doc["label"] == MOCK_LABEL
    assert doc["format"] == "MOCK_SYNTHETIC" and doc["refusal"] is None
    resp = client.get(proof["pdf_link"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["x-settlewell-proof-origin"] == "mock_synthetic"
    stored = _sql(path, "SELECT content, sha256 FROM proof_blob WHERE proof_document_id = 1")[0]
    assert resp.content == bytes(stored["content"])
    assert hashlib.sha256(resp.content).hexdigest() == stored["sha256"] == doc["sha256"]
    body = resp.content
    assert body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF") and len(body) > 200
    assert b"MOCK / SYNTHETIC" in body and body != b"PDF"
    offset = int(body.split(b"startxref\n")[1].split(b"\n")[0])
    assert body[offset:offset + 4] == b"xref"
    assert _dump(path) == before  # GETs never write


def test_a_valid_pdf_builder_has_exact_object_offsets():
    body = build_mock_pdf(["a (b) \\ c"])
    table = body.split(b"xref\n")[1].split(b"\n")
    for number in range(1, 6):
        offset = int(table[1 + number][:10])
        assert body[offset:].startswith(b"%d 0 obj" % number)


def _refused(client, doc=1, inc=1, status=409):
    resp = client.get(f"/api/incidents/{inc}/proof/{doc}/pdf")
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert not resp.content.startswith(b"%PDF")
    return resp.json()["error"]


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "RETURNED"])
def test_a_later_local_status_refuses_the_old_pdf_and_hides_the_url(demo, status):
    client, _, path = demo
    _sql(path, "UPDATE transfer_record SET status = ? WHERE id = 1", status, write=True)
    assert _refused(client)["code"] == "proof_not_eligible"
    proof = _link(client)
    assert proof["pdf_link"] is None and proof["documents"][0]["pdf_url"] is None
    assert proof["documents"][0]["refusal"] == "status_not_eligible"


def test_a_remote_only_failure_or_unreadable_remote_refuses(demo):
    client, _, path = demo
    _sql(path, "UPDATE mock_remote SET status = 'FAILED' WHERE airwallex_id = 'synthetic-inv-2291'",
         write=True)
    assert _refused(client)["gate_code"] == "proof_not_eligible:status_not_eligible"
    _sql(path, "DELETE FROM mock_remote WHERE airwallex_id = 'synthetic-inv-2291'", write=True)
    assert _refused(client)["gate_code"] == "proof_not_eligible:remote_unknown"
    assert _link(client)["pdf_link"] is None


def test_a_new_return_snapshot_refuses_the_old_pdf(demo):
    client, _, path = demo
    _sql(path, "INSERT INTO status_snapshot (transfer_record_id, status, source, seen_at) "
         "VALUES (1, 'RETURNED', 'poll', ?)", SEEN, write=True)
    assert _refused(client)["gate_code"] == "proof_not_eligible:status_not_eligible"


def test_a_pending_webhook_window_withholds_the_pdf(demo):
    client, _, path = demo
    _sql(path, "INSERT INTO webhook_state VALUES ('synthetic-inv-2291', 1, 'FAILED', 9999999999999, 'e1')",
         write=True)
    assert _refused(client)["gate_code"] == "proof_not_eligible:webhook_pending"
    assert _link(client)["pdf_link"] is None


def test_wrong_incident_wrong_document_and_traversal_are_refused(demo):
    client, _, path = demo
    _refused(client, doc=99, status=404)
    _refused(client, inc=99, status=404)
    # a document that belongs to another obligation's incident
    _sql(path, "INSERT INTO supplier (name, contact, phone_on_file) VALUES ('Other', 'o@x.example', '1')",
         write=True)
    _sql(path, "INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status) "
         "VALUES ('INV-9', '1.00', 'EUR', 2, 'OPEN')", write=True)
    _sql(path, "INSERT INTO transfer_record (airwallex_id, request_id, obligation_id, origin, details_hash,"
         " amount, currency, status, reference) VALUES ('o-1', 'r-o-1', 2, 'settlewell', 'h', '1.00',"
         " 'EUR', 'SENT', 'INV-9')", write=True)
    _sql(path, "INSERT INTO incident (obligation_id, claim_json, state) VALUES (2, NULL, 'OPEN')",
         write=True)
    other = build_mock_pdf(["other"])
    digest = hashlib.sha256(other).hexdigest()
    _sql(path, "INSERT INTO proof_document (id, transfer_record_id, format, status_at_issue, sha256)"
         " VALUES (7, 3, 'MOCK_SYNTHETIC', 'SENT', ?)", digest, write=True)
    _sql(path, "INSERT INTO proof_blob (proof_document_id, content, sha256, origin, label)"
         " VALUES (7, ?, ?, 'mock_synthetic', 'x')", other, digest, write=True)
    assert _refused(client, doc=7, inc=1, status=404)["code"] == "not_found"
    for bad in ("..%2f..%2fetc%2fpasswd", "1/../../x", "abc"):
        resp = client.get(f"/api/incidents/1/proof/{bad}/pdf")
        assert resp.status_code in (400, 404) and not resp.content.startswith(b"%PDF")


def test_tampered_missing_or_non_pdf_bytes_are_refused_and_unlinked(demo):
    client, _, path = demo
    _sql(path, "UPDATE proof_blob SET content = ? WHERE proof_document_id = 1", b"PDF", write=True)
    assert _refused(client)["code"] == "proof_integrity"
    assert _link(client)["documents"][0]["refusal"] == "hash_mismatch"
    # a self-consistent hash does not make a placeholder string a document
    _sql(path, "UPDATE proof_blob SET sha256 = ? WHERE proof_document_id = 1",
         hashlib.sha256(b"PDF").hexdigest(), write=True)
    _sql(path, "UPDATE proof_document SET sha256 = ? WHERE id = 1", hashlib.sha256(b"PDF").hexdigest(),
         write=True)
    assert _refused(client)["code"] == "proof_integrity"
    _sql(path, "DELETE FROM proof_blob", write=True)
    assert _refused(client, status=404)["code"] == "proof_unavailable"
    proof = _link(client)
    assert proof["pdf_link"] is None and proof["documents"][0]["refusal"] == "no_stored_bytes"


# ---- 3. replacement and FX approvals through the real execute() gate ----


class Remote(Funded):
    """Stateful remote: a payout debits EUR, a conversion credits EUR. Same client for every request."""

    def __init__(self, eur="10000.00", **kwargs):
        super().__init__(**kwargs)
        self.eur = Decimal(eur)

    def current_balances(self):
        self.balance_calls += 1
        return rows(str(self.eur))

    def create_transfer(self, **kwargs):
        new = super().create_transfer(**kwargs)
        self.eur -= kwargs["transfer_amount"]
        return new

    def create_conversion(self, **kwargs):
        out = super().create_conversion(**kwargs)
        self.eur += Decimal(self.buy_amount)
        return out


def _env(tmp_path, client):
    path = tmp_path / "p17.db"
    conn = connect(path)
    obligation_id, incident_id = _ready(conn)
    conn.close()
    app_client, headers = _boot(path, client_factory=lambda _conn: client, clock=lambda: SEEN)
    return app_client, headers, path, obligation_id, incident_id


def _open(path, incident_id, obligation_id, **kw):
    conn = connect(path)
    fields = {"kind": "replacement", "incident_id": incident_id, "obligation_id": obligation_id,
                  "action": "REPLACE_NEW_DETAILS", "amount": "4200.00", "currency": "EUR",
                  "beneficiary_id": "ben_verified", "details_hash": "callback-v2", "proposer": AP_CLERK,
                  "failure_code": "90701", "expires_at": FAR, "created_at": SEEN}
    fields.update(kw)
    ref = open_intent(conn, **fields)
    conn.commit()
    conn.close()
    return ref


def _post(app_client, headers, incident_id, intent_id):
    detail = app_client.get(f"/api/incidents/{incident_id}").json()
    view = next(p for p in detail["proposals"] if p["id"] == intent_id)
    return app_client.post(f"/api/incidents/{incident_id}/proposals/{intent_id}/approve",
                           json={"binding_hash": view["binding_hash"]}, headers=headers)


def test_api_approval_executes_one_replacement_and_replay_is_refused(tmp_path):
    remote = Remote()
    app_client, headers, path, ob, inc = _env(tmp_path, remote)
    ref = _open(path, inc, ob)
    resp = _post(app_client, headers, inc, ref.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["kind"] == "replacement" and resp.json()["result"]["remote_id"] == "awx-new"
    # remote: exactly one payout, locked amount/currency/beneficiary, balance debited
    assert len(remote.created) == 1 and remote.conversions == []
    sent = remote.created[0]
    assert (sent["transfer_amount"], sent["transfer_currency"], sent["beneficiary_id"]) == (
        Decimal("4200.00"), "EUR", "ben_verified")
    assert remote.eur == Decimal("5800.00")
    # ledger: intent executed, grant consumed once, new transfer recorded, funding durable
    intent = _sql(path, "SELECT state, remote_id, request_id FROM execution_intent WHERE id = ?", ref.id)[0]
    assert (intent["state"], intent["remote_id"]) == ("executed", "awx-new") and intent["request_id"]
    grant = _sql(path, "SELECT used_at FROM approval WHERE id = ?", ref.approval_id)[0]
    assert grant["used_at"]
    new = _sql(path, "SELECT * FROM transfer_record WHERE airwallex_id = 'awx-new'")[0]
    assert (new["amount"], new["currency"], new["request_id"]) == ("4200.00", "EUR", sent["request_id"])
    assert _sql(path, "SELECT COUNT(*) FROM payout_funding WHERE intent_id = ?", ref.id)[0][0] == 1
    assert _sql(path, "SELECT COUNT(*) FROM audit_event WHERE kind = 'intent_executed'")[0][0] == 1
    # replay refused, nothing more sent
    again = app_client.post(f"/api/incidents/{inc}/proposals/{ref.id}/approve",
                            json={"binding_hash": "0" * 64}, headers=headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "approval_used"
    assert len(remote.created) == 1 and remote.eur == Decimal("5800.00")


def test_wrong_incident_and_changed_terms_never_pay(tmp_path):
    remote = Remote()
    app_client, headers, path, ob, inc = _env(tmp_path, remote)
    ref = _open(path, inc, ob)
    view = app_client.get(f"/api/incidents/{inc}").json()["proposals"][0]
    wrong = app_client.post(f"/api/incidents/2/proposals/{ref.id}/approve",
                            json={"binding_hash": view["binding_hash"]}, headers=headers)
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "cross_incident"
    bad = app_client.post(f"/api/incidents/{inc}/proposals/{ref.id}/approve",
                          json={"binding_hash": "f" * 64}, headers=headers)
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "stale_terms"
    # terms change after the operator saw them: the hash they approve no longer matches
    _sql(path, "UPDATE execution_intent SET amount = '4201.00' WHERE id = ?", ref.id, write=True)
    changed = app_client.post(f"/api/incidents/{inc}/proposals/{ref.id}/approve",
                              json={"binding_hash": view["binding_hash"]}, headers=headers)
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "stale_terms"
    assert remote.created == [] and remote.eur == Decimal("10000.00")
    assert _sql(path, "SELECT used_at FROM approval WHERE id = ?", ref.approval_id)[0][0] is None


def test_a_funding_shortfall_is_a_clean_refusal_not_a_500(tmp_path):
    remote = Remote(eur="5226.93")  # one cent below amount + fee budget + floor
    app_client, headers, path, ob, inc = _env(tmp_path, remote)
    ref = _open(path, inc, ob)
    resp = _post(app_client, headers, inc, ref.id)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "gate_refused"
    assert resp.json()["error"]["gate_code"] == "balance"
    assert remote.created == [] and remote.eur == Decimal("5226.93")
    assert _sql(path, "SELECT state FROM execution_intent WHERE id = ?", ref.id)[0][0] == "refused"


def test_api_approval_converts_the_locked_fx_quote_once(tmp_path):
    remote = Remote(eur="0.00")
    app_client, headers, path, ob, inc = _env(tmp_path, remote)
    conn = connect(path)
    locked = fund_replacement(conn, remote, obligation_id=ob, as_of=SEEN)
    conn.close()
    assert locked.action == "locked" and remote.conversions == []
    remote.buy_amount = locked.buy_amount
    ref = _open(path, inc, ob, kind="fx_conversion", action="FX_FUND", quote_id=locked.quote_id,
                amount=format(locked.buy_amount, "f"), failure_code=None)
    # approval of a different incident or stale terms does not convert
    wrong = app_client.post(f"/api/incidents/2/proposals/{ref.id}/approve",
                            json={"binding_hash": "a" * 64}, headers=headers)
    assert wrong.status_code == 409 and remote.conversions == []
    resp = _post(app_client, headers, inc, ref.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"kind": "fx_conversion", "remote_id": "con_exec",
                                     "status": "converted", "funding_note": None}
    assert len(remote.conversions) == 1 and remote.conversions[0]["quote_id"] == locked.quote_id
    assert remote.eur == Decimal(locked.buy_amount)  # settlement credited the remote balance
    quote = _sql(path, "SELECT state, consumed_by FROM fx_quote WHERE quote_id = ?", locked.quote_id)[0]
    assert quote["state"] == "CONSUMED" and quote["consumed_by"]
    assert _sql(path, "SELECT used_at FROM approval WHERE id = ?", ref.approval_id)[0][0]
    assert _sql(path, "SELECT state FROM execution_intent WHERE id = ?", ref.id)[0][0] == "executed"
    again = app_client.post(f"/api/incidents/{inc}/proposals/{ref.id}/approve",
                            json={"binding_hash": "0" * 64}, headers=headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "approval_used"
    assert len(remote.conversions) == 1 and remote.eur == Decimal(locked.buy_amount)


def test_fx_with_changed_terms_does_not_convert(tmp_path):
    remote = Remote(eur="0.00")
    app_client, headers, path, ob, inc = _env(tmp_path, remote)
    conn = connect(path)
    locked = fund_replacement(conn, remote, obligation_id=ob, as_of=SEEN)
    conn.close()
    ref = _open(path, inc, ob, kind="fx_conversion", action="FX_FUND", quote_id=locked.quote_id,
                amount=format(locked.buy_amount, "f"), failure_code=None)
    view = app_client.get(f"/api/incidents/{inc}").json()["proposals"][0]
    _sql(path, "UPDATE execution_intent SET amount = '1.00' WHERE id = ?", ref.id, write=True)
    resp = app_client.post(f"/api/incidents/{inc}/proposals/{ref.id}/approve",
                           json={"binding_hash": view["binding_hash"]}, headers=headers)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "stale_terms"
    assert remote.conversions == [] and remote.eur == Decimal("0.00")


def test_fx_with_expired_quote_does_not_convert(tmp_path):
    import dataclasses

    class Requoting(Remote):
        """A replacement quote gets a fresh id, as the real service would issue."""

        quotes = 0

        def create_quote(self, **kwargs):
            quote = super().create_quote(**kwargs)
            self.quotes += 1
            return dataclasses.replace(quote, quote_id=f"{quote.quote_id}-{self.quotes}")

    remote = Requoting(eur="0.00")
    app_client, headers, path, ob, inc = _env(tmp_path, remote)
    conn = connect(path)
    locked = fund_replacement(conn, remote, obligation_id=ob, as_of=SEEN)
    conn.close()
    ref = _open(path, inc, ob, kind="fx_conversion", action="FX_FUND", quote_id=locked.quote_id,
                amount=format(locked.buy_amount, "f"), failure_code=None)
    view = app_client.get(f"/api/incidents/{inc}").json()["proposals"][0]
    _sql(path, "UPDATE fx_quote SET valid_to = '2000-01-01T00:00:00+00:00' WHERE quote_id = ?",
         locked.quote_id, write=True)
    resp = app_client.post(f"/api/incidents/{inc}/proposals/{ref.id}/approve",
                           json={"binding_hash": view["binding_hash"]}, headers=headers)
    assert resp.status_code == 409 and resp.json()["error"]["gate_code"] == "reapprove"
    assert remote.conversions == [] and remote.eur == Decimal("0.00")
    state = _sql(path, "SELECT state FROM execution_intent WHERE id = ?", ref.id)[0]["state"]
    assert state != "executed"


def test_routes_have_no_direct_money_call_and_default_client_is_cancel_only():
    import inspect

    source = inspect.getsource(api_module)
    for banned in ("create_transfer", "create_conversion", "create_payment", "create_beneficiary"):
        assert banned not in source
    default = api_module.LedgerMockClient
    assert not hasattr(default, "create_transfer") and not hasattr(default, "create_conversion")
    assert Pay  # helper imported for parity with test_execute

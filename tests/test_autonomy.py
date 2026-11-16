"""Risk-tiered autonomy. Offline only: mock API, ledger rows and the stateful fake remote."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend import autonomy
from backend.api import create_app
from backend.approvals import AP_CLERK
from backend.execute import ExecuteError, auto_approve_intent, execute, open_intent
from backend.ledger import connect

ORIGIN = "http://127.0.0.1:8765"
CANCEL_RULE = "A-CANCEL-SCHEDULED-DUPLICATE"


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")


def _on(monkeypatch, limit="5000"):
    monkeypatch.setenv("SETTLEWELL_AUTONOMY", "1")
    monkeypatch.setenv("SETTLEWELL_AUTO_MAX_AMOUNT", limit)


def _boot(tmp_path):
    path = tmp_path / "demo.db"
    client = TestClient(create_app(str(path)), base_url=ORIGIN)
    token = client.get("/api/session").json()["csrf_token"]
    return client, {"Origin": ORIGIN, "X-Settlewell-CSRF": token}, path


def _q(path, sql, *args):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _open(conn, kind, amount="100.00", currency="EUR", action=None, **kw):
    ob = conn.execute("SELECT obligation_id FROM incident WHERE id = 1").fetchone()[0]
    now = datetime.now(UTC)
    return open_intent(
        conn, kind=kind, incident_id=1, obligation_id=ob,
        action=action or {"replacement": "REPLACE_SAME", "fx_conversion": "FX_FUND",
                          "beneficiary": "NEW_BENEFICIARY"}.get(kind, kind.upper()),
        amount=amount, currency=currency, beneficiary_id="ben_x", details_hash="h",
        proposer=AP_CLERK, expires_at=(now + timedelta(hours=1)).isoformat(),
        created_at=now.isoformat(), **kw)


# ---- policy table ----

def test_policy_table_is_immutable_at_runtime():
    with pytest.raises(TypeError):
        autonomy.POLICY_TABLE["replacement"] = autonomy.POLICY_TABLE["cancel"]  # type: ignore[index]
    with pytest.raises(TypeError):
        del autonomy.POLICY_TABLE["cancel"]  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        autonomy.POLICY_TABLE["replacement"].tier = autonomy.AUTO  # type: ignore[misc]
    with pytest.raises(AttributeError):
        autonomy.NEVER_AUTO.add("cancel")  # type: ignore[attr-defined]


def test_money_and_beneficiary_kinds_are_human_in_the_table():
    for kind in ("replacement", "fx_conversion", "beneficiary"):
        assert kind in autonomy.NEVER_AUTO
        assert autonomy.POLICY_TABLE[kind].tier == autonomy.HUMAN
    assert autonomy.POLICY_TABLE["cancel"].tier == autonomy.AUTO


def test_autonomy_never_reads_model_output():
    import inspect

    source = inspect.getsource(autonomy)
    for banned in ("advisory", "claims", "llm_rationale", "anthropic"):
        assert f"import {banned}" not in source and f"backend.{banned}" not in source
    assert "llm_rationale" not in source


def test_defaults_off_and_eur_1000():
    assert autonomy.enabled({}) is False
    assert autonomy.max_amount({}) == Decimal(1000)
    assert autonomy.max_amount({"SETTLEWELL_AUTO_MAX_AMOUNT": "junk"}) == Decimal(0)
    assert autonomy.max_amount({"SETTLEWELL_AUTO_MAX_AMOUNT": "-5"}) == Decimal(0)


# ---- never auto-pay / beneficiary / FX ----

@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(kind=st.sampled_from(sorted(autonomy.NEVER_AUTO)),
       amount=st.decimals(min_value=Decimal("0.01"), max_value=Decimal(999999), places=2),
       currency=st.sampled_from(["EUR", "USD", "GBP"]))
def test_property_money_kinds_never_auto(tmp_path_factory, monkeypatch, kind, amount, currency):
    _on(monkeypatch, "99999999")
    path = tmp_path_factory.mktemp("p") / "demo.db"
    create_app(str(path))
    conn = connect(str(path))
    try:
        ref = _open(conn, kind, amount=str(amount), currency=currency)
        row = conn.execute("SELECT * FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
        assert autonomy.classify_intent(conn, row).tier == autonomy.HUMAN
        assert auto_approve_intent(conn, ref.id, as_of=datetime.now(UTC).isoformat()) is None
        used = conn.execute("SELECT used_at, approver FROM approval WHERE id = ?", (ref.approval_id,)).fetchone()
        assert used["used_at"] is None and used["approver"] is None
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["replacement", "fx_conversion"])
def test_forged_policy_approval_on_money_kind_is_refused_by_execute(tmp_path, monkeypatch, kind):
    _on(monkeypatch)
    _client, _h, path = _boot(tmp_path)
    conn = connect(str(path))
    try:
        ref = _open(conn, kind, quote_id="q1" if kind == "fx_conversion" else None)
        conn.execute("UPDATE approval SET approver = ?, used_at = ? WHERE id = ?",
                     (f"policy:{CANCEL_RULE}", datetime.now(UTC).isoformat(), ref.approval_id))
        conn.commit()
        with pytest.raises(ExecuteError) as err:
            execute(conn, object(), ref.id, as_of=datetime.now(UTC).isoformat())
        assert err.value.code == "policy_auto_forbidden"
    finally:
        conn.close()


def test_forged_policy_rule_mismatch_on_cancel_is_refused(tmp_path, monkeypatch):
    _on(monkeypatch)
    _client, _h, path = _boot(tmp_path)
    conn = connect(str(path))
    try:
        intent = conn.execute("SELECT * FROM execution_intent WHERE kind = 'cancel'").fetchone()
        conn.execute("UPDATE approval SET approver = 'policy:A-MADE-UP', used_at = ? WHERE id = ?",
                     (datetime.now(UTC).isoformat(), intent["approval_id"]))
        conn.commit()
        with pytest.raises(ExecuteError) as err:
            execute(conn, object(), int(intent["id"]), as_of=datetime.now(UTC).isoformat())
        assert err.value.code == "policy_auto_forbidden"
    finally:
        conn.close()


# ---- threshold, status, currency, unknown ----

@settings(max_examples=40, deadline=None)
@given(amount=st.decimals(min_value=Decimal("0.01"), max_value=Decimal(20000), places=2),
       limit=st.decimals(min_value=Decimal(0), max_value=Decimal(20000), places=2))
def test_property_threshold_respected(amount, limit):
    rule = autonomy.POLICY_TABLE["cancel"]
    verdict = autonomy._amount_verdict(str(amount), "EUR", rule, {"SETTLEWELL_AUTO_MAX_AMOUNT": str(limit)})
    assert verdict.auto is (amount <= limit)


def test_cancel_over_threshold_or_not_scheduled_or_foreign_currency_needs_human(tmp_path, monkeypatch):
    _on(monkeypatch, "1000")  # the seeded duplicate is EUR 4,200
    _client, _h, path = _boot(tmp_path)
    conn = connect(str(path))
    try:
        row = conn.execute("SELECT * FROM execution_intent WHERE kind = 'cancel'").fetchone()
        v = autonomy.classify_intent(conn, row)
        assert v.tier == autonomy.HUMAN and v.rule_id == "H-OVER-THRESHOLD"
        assert auto_approve_intent(conn, int(row["id"]), as_of=datetime.now(UTC).isoformat()) is None
        _on(monkeypatch, "5000")
        assert autonomy.classify_intent(conn, row).auto
        conn.execute("UPDATE transfer_record SET status = 'SENT' WHERE id = ?", (row["target_id"],))
        v = autonomy.classify_intent(conn, row)
        assert v.tier == autonomy.HUMAN and v.rule_id == "R-NO-STOP-NOT-SCHEDULED"
        conn.execute("UPDATE transfer_record SET status = 'SCHEDULED' WHERE id = ?", (row["target_id"],))
        fake = dict(row) | {"currency": "USD"}
        assert autonomy.classify_intent(conn, fake).tier == autonomy.HUMAN
    finally:
        conn.close()


def test_unknown_kind_is_human():
    v = autonomy.classify_intent(None, {"kind": "something_new", "amount": "1", "currency": "EUR"},
                                 env={"SETTLEWELL_AUTONOMY": "1"})
    assert v.tier == autonomy.HUMAN and v.rule_id == "H-UNRECOGNISED"


def test_outreach_with_money_is_human():
    env = {"SETTLEWELL_AUTONOMY": "1"}
    assert autonomy.classify_intent(None, {"kind": "outreach", "amount": "0"}, env=env).auto
    assert not autonomy.classify_intent(None, {"kind": "outreach", "amount": "5"}, env=env).auto


# ---- kill switch and API ----

def test_kill_switch_off_keeps_todays_behaviour(tmp_path):
    client, headers, path = _boot(tmp_path)  # conftest: SETTLEWELL_AUTONOMY=0
    detail = client.get("/api/incidents/1").json()
    prop = detail["proposals"][0]
    assert prop["autonomy"]["tier"] == autonomy.HUMAN and prop["can_approve"] is True
    assert detail["autonomy"]["enabled"] is False
    resp = client.post("/api/incidents/1/agent/run", headers=headers)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "autonomy_off"
    assert _q(path, "SELECT COUNT(*) FROM approval WHERE used_at IS NOT NULL")[0][0] == 0
    ok = client.post(f"/api/incidents/1/proposals/{prop['id']}/approve", headers=headers,
                     json={"binding_hash": prop["binding_hash"]})
    assert ok.status_code == 200
    assert _q(path, "SELECT approver FROM approval")[0][0] == "finance-approver"


def test_agent_run_auto_cancels_duplicate_once_with_policy_approver(tmp_path, monkeypatch):
    _on(monkeypatch)
    client, headers, path = _boot(tmp_path)
    before = client.get("/api/incidents/1").json()
    assert before["proposals"][0]["autonomy"]["tier"] == autonomy.AUTO
    step = next(s for s in before["autonomy"]["steps"] if s["kind"] == "cancel")
    assert step["state"] == "auto_eligible"
    resp = client.post("/api/incidents/1/agent/run", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [h["rule_id"] for h in body["handled"]] == [CANCEL_RULE]
    assert _q(path, "SELECT approver FROM approval")[0][0] == f"policy:{CANCEL_RULE}"
    assert _q(path, "SELECT status FROM mock_remote WHERE airwallex_id='synthetic-dup-2291'")[0][0] == "CANCELLED"
    audit = [r[0] for r in _q(path, "SELECT detail FROM audit_event")]
    assert any(f"auto-approved by policy (rule {CANCEL_RULE})" in d for d in audit)
    after = client.get("/api/incidents/1").json()
    tl = after["autonomy"]
    cancel = next(s for s in tl["steps"] if s["kind"] == "cancel")
    assert cancel["tier"] == autonomy.AUTO and cancel["actor"] == f"policy:{CANCEL_RULE}"
    assert tl["auto"] == tl["total"] - tl["human"] and tl["auto"] >= 2
    again = client.post("/api/incidents/1/agent/run", headers=headers).json()
    assert again["handled"] == []  # single-use: nothing left to approve


def test_agent_run_leaves_human_gated_proposals_untouched(tmp_path, monkeypatch):
    _on(monkeypatch, "99999999")
    client, headers, path = _boot(tmp_path)
    conn = connect(str(path))
    try:
        ref = _open(conn, "replacement", amount="10.00")
    finally:
        conn.close()
    body = client.post("/api/incidents/1/agent/run", headers=headers).json()
    assert ref.id in body["needs_human"]
    row = _q(path, "SELECT used_at, approver FROM approval WHERE id = ?", ref.approval_id)[0]
    assert row["used_at"] is None and row["approver"] is None
    tl = client.get("/api/incidents/1").json()["autonomy"]
    rep = next(s for s in tl["steps"] if s["kind"] == "replacement")
    assert rep["tier"] == autonomy.HUMAN and rep["state"] == "waiting"
    assert tl["human"] >= 1 and tl["human_reasons"]


# ---- hosted hero ----

def test_hero_auto_cancels_duplicate_and_keeps_money_steps_human(tmp_path, monkeypatch):
    from tests.test_hero_live import Remote, _hero

    _on(monkeypatch)
    hero = _hero(tmp_path, Remote())
    result = hero.run()
    assert all(v.startswith(("PASS", "PARTIAL")) for v in result.values()), result
    rows = hero.conn.execute(
        "SELECT e.kind, a.approver FROM execution_intent e JOIN approval a ON a.id = e.approval_id").fetchall()
    by_kind = {r["kind"]: r["approver"] for r in rows}
    assert by_kind["cancel"] == f"policy:{CANCEL_RULE}"
    for kind in ("beneficiary", "replacement", "fx_conversion"):
        if kind in by_kind:
            assert not autonomy.is_policy_approver(by_kind[kind])
    ob = int(hero.m["ids"]["obligation_row"])
    tl = autonomy.timeline(hero.conn, ob, sandbox=True)
    assert tl["auto"] >= 4 and tl["human"] >= 2
    assert any("simulates this human approval" in s["detail"] for s in tl["steps"] if s["tier"] == "human")


def test_hero_with_autonomy_off_uses_human_approver(tmp_path):
    from tests.test_hero_live import Remote, _hero

    hero = _hero(tmp_path, Remote())
    hero.run()
    approver = hero.conn.execute(
        "SELECT a.approver FROM execution_intent e JOIN approval a ON a.id = e.approval_id "
        "WHERE e.kind = 'cancel'").fetchone()[0]
    assert approver == "finance-approver"


def test_cancel_targeting_the_live_payment_is_human(tmp_path, monkeypatch):
    _on(monkeypatch)
    _client, _h, path = _boot(tmp_path)
    conn = connect(str(path))
    try:
        row = conn.execute("SELECT * FROM execution_intent WHERE kind = 'cancel'").fetchone()
        live = conn.execute("SELECT live_transfer_id FROM obligation WHERE id = ?", (row["obligation_id"],)).fetchone()[0]
        conn.execute("UPDATE transfer_record SET status = 'SCHEDULED' WHERE id = ?", (live,))
        fake = dict(row) | {"target_id": str(live)}
        assert autonomy.classify_intent(conn, fake).tier == autonomy.HUMAN
    finally:
        conn.close()

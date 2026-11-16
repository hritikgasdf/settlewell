"""Advisory AI: template fallbacks, Claude path with a fake client, and the money boundary."""

import ast
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import advisory, claims
from backend import execute as execute_module
from backend.api import create_app
from backend.policy import Action

ORIGIN = "http://127.0.0.1:8765"
BACKEND = Path(__file__).resolve().parents[1] / "backend"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    advisory.clear_cache()
    yield
    advisory.clear_cache()


def _facts(**over):
    base = {
        "incident_id": 1, "incident_state": "OPEN", "supplier": "Lumen Textiles GmbH",
        "invoice_ref": "INV-2291", "amount": "4200.00", "currency": "EUR",
        "obligation_status": "OPEN", "claim": {"matched": True, "rule_id": "R-MATCHED"},
        "transfers": [{"id": 1, "status": "SENT", "failure_code": None, "amount": "4200.00",
                       "live": True},
                      {"id": 2, "status": "SCHEDULED", "failure_code": None,
                       "amount": "4200.00", "live": False}],
        "decisions": [{"rule_id": "R-STOP-DUPLICATE-SCHEDULED", "action": "STOP_DUPLICATE"}],
        "proposals": [{"action": "STOP_DUPLICATE", "state": "proposed"}],
        "needs_triage": False, "playbooks": advisory.PLAYBOOKS,
    }
    base.update(over)
    return base


def _reply(payload, model="claude-haiku-4-5-20251001"):
    return lambda facts: (json.dumps(payload), model)


def test_playbooks_mirror_policy_actions():
    assert set(advisory.PLAYBOOKS) == {a.value for a in Action}


def test_template_when_mock_even_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    monkeypatch.setattr(claims, "_make_client", lambda key: pytest.fail("no model in MOCK"))
    out = advisory.advise(_facts())
    assert out.explanation.source == "template" and out.reply_draft.source == "template"
    assert "R-STOP-DUPLICATE-SCHEDULED" in out.explanation.text
    assert out.triage.needed is False and out.triage.source == "rules"
    assert out.advisory_only is True


def test_template_when_no_key(monkeypatch):
    monkeypatch.delenv("SETTLEWELL_MOCK")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claims, "_make_client", lambda key: pytest.fail("no key, no call"))
    assert advisory.advise(_facts()).explanation.source == "template"


class _FakeClient:
    def __init__(self, text, calls):
        self.text, self.calls = text, calls
        self.messages = self

    def with_options(self, **kw):
        assert kw["timeout"] <= 20 and kw["max_retries"] == 0
        return self

    def create(self, **kw):
        self.calls.append(kw)
        assert kw["max_tokens"] <= 800
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                               content=[SimpleNamespace(type="text", text=self.text)])


def test_claude_path_with_fake_client_and_cache(monkeypatch):
    monkeypatch.delenv("SETTLEWELL_MOCK")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    calls = []
    text = json.dumps({"explanation": "Duplicate is scheduled, so rules cancel it.",
                       "reply_draft": "Hello, you will be paid once.", "triage": None})
    monkeypatch.setattr(claims, "_make_client", lambda key: _FakeClient(text, calls))
    first = advisory.advise(_facts())
    second = advisory.advise(_facts())
    assert first is second and len(calls) == 1  # cached per incident facts
    assert first.explanation.source == "claude" and first.reply_draft.source == "claude"
    assert first.triage.source == "rules"
    assert calls[0]["model"] == claims.model_name()


@pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({"explanation": 5})])
def test_bad_model_output_falls_back(raw):
    out = advisory.advise(_facts(), complete=lambda f: (raw, "m"))
    assert out.explanation.source == "template" and out.reply_draft.source == "template"


def test_model_error_falls_back():
    def boom(facts):
        raise TimeoutError

    out = advisory.advise(_facts(), complete=boom)
    assert out.explanation.source == "template" and out.model is None


def test_triage_only_for_unknown_and_validated():
    unknown = _facts(decisions=[{"rule_id": "R-UNKNOWN-CODE", "action": "ESCALATE"}],
                     needs_triage=True)
    good = advisory.advise(unknown, complete=_reply(
        {"explanation": "x", "reply_draft": "y",
         "triage": {"playbook": "REPLACE_SAME", "confidence": 0.6, "reason": "returned"}}))
    assert good.triage.source == "claude" and good.triage.playbook == "REPLACE_SAME"
    advisory.clear_cache()
    bad = advisory.advise(unknown, complete=_reply(
        {"explanation": "x", "reply_draft": "y",
         "triage": {"playbook": "SEND_MONEY_NOW", "confidence": 2}}))
    assert bad.triage.source == "template" and bad.triage.playbook in advisory.PLAYBOOKS
    advisory.clear_cache()
    known = advisory.advise(_facts(), complete=_reply(
        {"explanation": "x", "reply_draft": "y",
         "triage": {"playbook": "ESCALATE", "confidence": 1, "reason": "r"}}))
    assert known.triage.needed is False and known.triage.source == "rules"


def test_facts_flag_unknown_incidents():
    detail = {"incident": {"id": 3, "claim": None}, "decisions": [], "transfers": [],
              "proposals": [], "obligation": {}, "supplier": {"name": "S"}}
    assert advisory.facts_from_detail(detail)["needs_triage"] is True


def test_outputs_are_plain_strings():
    out = advisory.advise(_facts()).to_dict()

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        else:
            assert v is None or isinstance(v, (str, bool, float))

    walk(out)


def test_no_money_module_imports_advisory():
    """Only the read-only API route may import advisory; money code never sees its output."""
    importers = []
    for path in BACKEND.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.endswith("advisory") for n in names):
                importers.append(path.name)
    assert sorted(set(importers)) == ["api.py"]


def test_advisory_imports_no_money_code():
    tree = ast.parse((BACKEND / "advisory.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    project = {m for m in mods if m and m.startswith(("backend", "settlewell"))}
    assert project <= {"backend", "settlewell.flags"}  # backend.claims only, for the client
    source = (BACKEND / "advisory.py").read_text()
    for forbidden in (".execute(", "sqlite", "INSERT ", "UPDATE ", "DELETE ", "conn"):
        assert forbidden not in source


def _db_digest(path):
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        h = hashlib.sha256()
        for t in tables:
            h.update(repr(conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()).encode())
        return h.hexdigest()
    finally:
        conn.close()


def test_endpoint_is_read_only_and_never_executes(tmp_path, monkeypatch):
    path = tmp_path / "demo.db"
    client = TestClient(create_app(str(path)), base_url=ORIGIN)
    before = _db_digest(path)
    monkeypatch.setattr(execute_module, "execute",
                        lambda *a, **k: pytest.fail("advisory must never execute"))
    res = client.get("/api/incidents/1/advisory")
    assert res.status_code == 200
    body = res.json()
    assert body["advisory_only"] is True
    assert body["explanation"]["source"] == "template"
    assert "INV-2291" in body["reply_draft"]["text"]
    assert _db_digest(path) == before
    assert client.get("/api/incidents/999/advisory").status_code == 404

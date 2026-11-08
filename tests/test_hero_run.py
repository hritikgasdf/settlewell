"""P2-8 hero script and owned-only reset. Offline; fake clients only."""

from types import SimpleNamespace

import pytest

from backend.scripts import hero_run


def test_mock_run_passes_labelled_mock(monkeypatch, capsys):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    assert hero_run.main(["--mock"]) == 0
    out = capsys.readouterr().out
    assert "(MOCK" in out
    result = hero_run.run_mock()
    passed = {b for b, v in result.items() if v.startswith("PASS")}
    assert passed <= {"approved_replacement", "reconcile_both"}  # no blanket PASS
    for beat in ("fx_quote_conversion_settled", "bank_change_validated", "stop_duplicate",
                 "proof_outbox_status"):
        assert result[beat].startswith("PARTIAL")


def test_sandbox_uses_real_claude_never_mock_fallback(monkeypatch, tmp_path):
    from backend import claims

    seen = {}

    class _Hero:
        def __init__(self, *_a, complete=None, **_k):
            seen["complete"] = complete

        def run(self):
            return {}

    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    monkeypatch.setattr(hero_run, "Hero", _Hero)
    env = {"AIRWALLEX_SANDBOX_CLIENT_ID": "i", "AIRWALLEX_SANDBOX_API_KEY": "k"}
    hero_run.run_sandbox(str(tmp_path / "m.json"), env=env, transport=object())
    assert seen["complete"] is claims.anthropic_complete


def test_sandbox_needs_confirm(capsys):
    assert hero_run.main(["--sandbox"]) == 2


def test_sandbox_without_credentials_is_unverified_not_pass(monkeypatch, tmp_path):
    monkeypatch.delenv("AIRWALLEX_SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("AIRWALLEX_SANDBOX_CLIENT_ID", raising=False)
    result = hero_run.run_sandbox(str(tmp_path / "m.json"), env={})
    assert all(v.startswith("UNVERIFIED") for v in result.values())


def test_manifest_rejects_unsafe_ids_and_unknown_keys(tmp_path):
    path = str(tmp_path / "m.json")
    with pytest.raises(ValueError):
        hero_run.save_manifest(path, {"transfers": {"bad id; drop": "SENT"}})
    hero_run.save_manifest(path, {"transfers": {"tr_1": "SENT"}, "api_key": "secret"})
    assert hero_run.load_manifest(path) == {"transfers": {"tr_1": "SENT"}}


class _Client:
    def __init__(self, status_after):
        self.status, self.after, self.gets = "SENT", status_after, []

    def get_transfer(self, tid):
        self.gets.append(tid)
        return SimpleNamespace(status=self.status, request_id=self.req)

    req = "req_owned"

    def list_transfers(self, request_id=None):
        return SimpleNamespace(items=[])


def _run_db(tmp_path, ref="INV-12345678"):
    import sqlite3

    db = tmp_path / "run.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE obligation (id INTEGER PRIMARY KEY, invoice_ref TEXT)")
    conn.execute("INSERT INTO obligation (invoice_ref) VALUES (?)", (ref,))
    conn.commit()
    conn.close()
    return db


def test_reset_never_deletes_unbound_or_foreign_files(tmp_path):
    path = str(tmp_path / "m.json")
    victim = tmp_path / "important.db"
    victim.write_text("not sqlite")
    hero_run.save_manifest(path, {"requests": {"orig": "req_owned"}, "run": "12345678", "db": str(victim)})
    ok, problems = hero_run.reset_owned(path, _Client("PAID"), _Driver(_Client("PAID"), "PAID"))
    assert not ok and problems == ["db:unbound_not_deleted"] and victim.exists()
    outside = tmp_path / "sub"
    outside.mkdir()
    other = _run_db(outside)
    hero_run.save_manifest(path, {"requests": {"orig": "req_owned"}, "run": "12345678", "db": str(other)})
    assert not hero_run.reset_owned(path, _Client("PAID"), _Driver(_Client("PAID"), "PAID"))[0]
    assert other.exists()
    wrong = _run_db(tmp_path, ref="INV-99999999")  # another run's / foreign data
    hero_run.save_manifest(path, {"requests": {"orig": "req_owned"}, "run": "12345678", "db": str(wrong)})
    assert not hero_run.reset_owned(path, _Client("PAID"), _Driver(_Client("PAID"), "PAID"))[0]
    assert wrong.exists()


def test_reset_finds_unsaved_transfer_by_request_id(tmp_path):
    path = str(tmp_path / "m.json")
    hero_run.save_manifest(path, {"requests": {"orig": "req_owned"}, "run": "12345678"})

    class _C(_Client):
        def list_transfers(self, request_id=None):
            return SimpleNamespace(items=[SimpleNamespace(id="tr_orphan", request_id="req_owned")])

    client = _C("PAID")
    driver = _Driver(client, "PAID")
    assert hero_run.reset_owned(path, client, driver) == (True, [])
    assert driver.released == ["tr_orphan"]


class _Driver:
    def __init__(self, client, final):
        self.client, self.final, self.released = client, final, []

    def release_sent(self, tid):
        self.released.append(tid)
        self.client.status = self.final


def test_reset_touches_only_owned_and_clears_after_readback(tmp_path):
    path = str(tmp_path / "m.json")
    db = _run_db(tmp_path)
    other = tmp_path / "other.db"
    other.write_text("keep")
    hero_run.save_manifest(path, {"transfers": {"tr_owned": "SENT"}, "requests": {"orig": "req_owned"},
                                  "run": "12345678", "db": str(db)})
    client = _Client("PAID")
    driver = _Driver(client, "PAID")
    assert hero_run.reset_owned(path, client, driver) == (True, [])
    assert driver.released == ["tr_owned"] and not db.exists() and other.exists()


def test_reset_refuses_foreign_ids_and_keeps_everything(tmp_path):
    path = str(tmp_path / "m.json")
    db = _run_db(tmp_path)
    hero_run.save_manifest(path, {"transfers": {"tr_x": "SENT"}, "requests": {"orig": "mine"},
                                  "run": "12345678", "db": str(db)})
    client = _Client("PAID")
    driver = _Driver(client, "PAID")
    ok, problems = hero_run.reset_owned(path, client, driver)
    assert not ok and problems == ["tr_x:foreign"] and driver.released == [] and db.exists()


def test_reset_demo_wrapper_refuses_account_wide(capsys):
    from backend.scripts import reset_demo

    assert reset_demo.main(["--all"]) == 2


def test_reset_refuses_when_still_in_flight(tmp_path):
    path = str(tmp_path / "m.json")
    db = _run_db(tmp_path)
    hero_run.save_manifest(path, {"transfers": {"tr_owned": "SENT"}, "requests": {"orig": "req_owned"},
                                  "run": "12345678", "db": str(db)})
    client = _Client("SENT")
    ok, problems = hero_run.reset_owned(path, client, _Driver(client, "SENT"))
    assert not ok and problems == ["tr_owned:SENT"] and db.exists()
    assert hero_run.load_manifest(path)["transfers"]

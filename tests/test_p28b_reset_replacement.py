"""Reset recognises this run's own executed replacement from the BOUND ledger, never blindly."""
import sqlite3
from types import SimpleNamespace

from test_hero_run import _Client, _Driver, _run_db

from backend.scripts import hero_run

REQ = "stl-inv12345678-1"


def _db(tmp_path, state="executed", ref="INV-12345678"):
    db = _run_db(tmp_path, ref=ref)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE execution_intent (id INTEGER PRIMARY KEY, kind TEXT, obligation_id INTEGER, "
                 "state TEXT, remote_id TEXT, request_id TEXT)")
    conn.execute("INSERT INTO execution_intent (kind, obligation_id, state, remote_id, request_id) "
                 "VALUES ('replacement', 1, ?, 'tr_repl', ?)", (state, REQ))
    conn.commit()
    conn.close()
    return db


class _Repl(_Client):
    req = REQ


def _manifest(tmp_path, db, transfers):
    path = str(tmp_path / "m.json")
    hero_run.save_manifest(path, {"transfers": transfers, "requests": {"orig": "req_owned"},
                                  "run": "12345678", **({"db": str(db)} if db else {})})
    return path


def test_own_executed_replacement_is_owned_and_reset_succeeds(tmp_path):
    db = _db(tmp_path)
    path = _manifest(tmp_path, db, {"tr_repl": "PAID"})
    client = _Repl("PAID")
    assert hero_run.reset_owned(path, client, _Driver(client, "PAID")) == (True, [])
    assert not db.exists()


def test_replacement_found_by_durable_request_id_before_manifest_id_save(tmp_path):
    db = _db(tmp_path)
    path = _manifest(tmp_path, db, {})

    class _C(_Repl):
        def list_transfers(self, request_id=None):
            hit = [SimpleNamespace(id="tr_repl", request_id=request_id)] if request_id == REQ else []
            return SimpleNamespace(items=hit)

    client = _C("PAID")
    driver = _Driver(client, "PAID")
    assert hero_run.reset_owned(path, client, driver) == (True, [])
    assert driver.released == ["tr_repl"]


def test_foreign_remote_is_still_rejected_and_nothing_deleted(tmp_path):
    db = _db(tmp_path)
    path = _manifest(tmp_path, db, {"tr_x": "PAID"})
    client = _Client("PAID")
    client.req = "someone-else"
    ok, problems = hero_run.reset_owned(path, client, _Driver(client, "PAID"))
    assert not ok and problems == ["tr_x:foreign"] and db.exists()


def test_unexecuted_or_other_run_ledger_does_not_grant_ownership(tmp_path):
    for kwargs in ({"state": "proposed"}, {"ref": "INV-99999999"}):
        db = _db(tmp_path, **kwargs)
        path = _manifest(tmp_path, db, {"tr_repl": "PAID"})
        ok, _problems = hero_run.reset_owned(path, _Repl("PAID"), _Driver(_Repl("PAID"), "PAID"))
        assert not ok and db.exists()
        db.unlink()


def test_no_bound_db_fails_closed_as_foreign(tmp_path):
    path = _manifest(tmp_path, None, {"tr_repl": "PAID"})
    ok, problems = hero_run.reset_owned(path, _Repl("PAID"), _Driver(_Repl("PAID"), "PAID"))
    assert not ok and problems == ["tr_repl:foreign"]

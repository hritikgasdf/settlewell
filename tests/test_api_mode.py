"""P2-8 API modes. No network: the sandbox client is built with a stub transport and never called."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.airwallex.client import AirwallexClient
from backend.api import LedgerMockClient, create_app
from backend.api_mode import (
    API_KEY_ENV,
    BASE_URL_ENV,
    CLIENT_ID_ENV,
    exact_sandbox_url,
    make_client_factory,
    resolve_mode,
)

ORIGIN = "http://127.0.0.1:8765"
GOOD = {CLIENT_ID_ENV: "id-placeholder", API_KEY_ENV: "key-placeholder"}


class _NoNetwork:
    def send(self, *_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("network used")


@pytest.mark.parametrize("url", [
    "https://api.airwallex.com", "http://api-demo.airwallex.com", "https://api-demo.airwallex.com:444",
    "https://api-demo.airwallex.com.evil.test", "https://x@api-demo.airwallex.com",
    "https://api-demo.airwallex.com/api/v1", "", "https://api-demo.airwallex.com?x=1",
])
def test_only_exact_sandbox_url(url):
    assert not exact_sandbox_url(url)
    assert resolve_mode("sandbox", {**GOOD, BASE_URL_ENV: url}).mode == "READ_ONLY"


def test_mode_resolution():
    assert resolve_mode("mock", {}).mode == "MOCK"
    ok = resolve_mode("sandbox", GOOD)
    assert (ok.mode, ok.writes_enabled) == ("SANDBOX", True)
    assert exact_sandbox_url("https://api-demo.airwallex.com/")
    partial = resolve_mode("sandbox", {CLIENT_ID_ENV: "x"})
    assert (partial.mode, partial.writes_enabled, partial.missing) == ("READ_ONLY", False, (API_KEY_ENV,))
    assert resolve_mode("live", GOOD).writes_enabled is False
    assert "key-placeholder" not in repr(ok) + repr(partial)


def test_adapter_selection(tmp_path):
    assert make_client_factory(resolve_mode("mock", {})) is LedgerMockClient
    client = make_client_factory(resolve_mode("sandbox", GOOD), GOOD, transport=_NoNetwork())(None)
    assert isinstance(client, AirwallexClient)
    assert client._base_url == "https://api-demo.airwallex.com"
    with pytest.raises(PermissionError):
        make_client_factory(resolve_mode("sandbox", {}))(None)


def _app(tmp_path, config):
    app = create_app(str(tmp_path / "s.db"), mode_config=config,
                     client_factory=make_client_factory(config, GOOD, transport=_NoNetwork()))
    return TestClient(app, base_url=ORIGIN)


def test_sandbox_mode_labels_and_never_seeds_mock(tmp_path, monkeypatch):
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    client = _app(tmp_path, resolve_mode("sandbox", GOOD))
    body = client.get("/api/health", headers={"host": "127.0.0.1:8765"}).json()
    assert body["mode"] == "SANDBOX" and body["writes_enabled"] is True
    assert "MOCK" not in body["banner"] and "not production" in body["banner"]
    assert not (tmp_path / "s.db").exists()  # no demo seed, no mock_remote


def test_incomplete_credentials_are_read_only_and_writes_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    client = _app(tmp_path, resolve_mode("sandbox", {}))
    body = client.get("/api/health").json()
    assert body["mode"] == "READ_ONLY" and body["writes_enabled"] is False
    token = client.get("/api/session").json()["csrf_token"]
    resp = client.post("/api/incidents/1/proposals/1/approve", json={"binding_hash": "0" * 64},
                       headers={"Origin": ORIGIN, "X-Settlewell-CSRF": token})
    assert resp.status_code == 503


def test_host_guard_stays_in_sandbox(tmp_path):
    client = _app(tmp_path, resolve_mode("sandbox", GOOD))
    assert client.get("/api/health", headers={"host": "evil.test"}).status_code == 403


def test_mock_default_still_seeds(tmp_path, monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    client = _app(tmp_path, resolve_mode("mock", {}))
    assert client.get("/api/health").json()["mode"] == "MOCK"
    conn = sqlite3.connect(tmp_path / "s.db")
    assert conn.execute("SELECT COUNT(*) FROM mock_remote").fetchone()[0] > 0


def _read_only_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    path = str(tmp_path / "ro.db")
    from backend.api import init_db

    init_db(path)
    monkeypatch.delenv("SETTLEWELL_MOCK")
    config = resolve_mode("sandbox", {})
    app = create_app(path, mode_config=config, client_factory=make_client_factory(config))
    return TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)


def test_read_only_proof_gets_fail_closed_503_not_500(tmp_path, monkeypatch):
    from backend import api_proof

    def _needs_remote(conn, make_client, *_a):
        make_client(conn)  # the READ_ONLY factory raises PermissionError

    monkeypatch.setattr(api_proof, "proof_view", _needs_remote)
    monkeypatch.setattr(api_proof, "load_pdf", _needs_remote)
    client = _read_only_app(tmp_path, monkeypatch)
    for url in ("/api/incidents/1", "/api/incidents/1/proof/1/pdf"):
        resp = client.get(url)
        assert resp.status_code == 503 and resp.json()["error"]["code"] == "read_only", url


def test_read_only_real_proof_path_never_500(tmp_path, monkeypatch):
    client = _read_only_app(tmp_path, monkeypatch)
    for url in ("/api/incidents/1", "/api/incidents/1/proof/1/pdf"):
        assert client.get(url).status_code in (200, 404, 409, 503), url


def _ui_app(tmp_path, monkeypatch, ui_port):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    app = create_app(str(tmp_path / "c.db"), ui_port=ui_port, mode_config=resolve_mode("mock", {}))
    return TestClient(app, base_url=ORIGIN)


def _approve(client, origin):
    token = client.get("/api/session").json()["csrf_token"]
    return client.post("/api/incidents/1/proposals/1/approve", json={"binding_hash": "0" * 64},
                       headers={"Origin": origin, "X-Settlewell-CSRF": token})


def test_custom_ui_origin_allowed_and_foreign_rejected(tmp_path, monkeypatch):
    client = _ui_app(tmp_path, monkeypatch, 15173)
    for origin in ("http://127.0.0.1:15173", "http://localhost:15173"):
        assert _approve(client, origin).status_code != 403, origin
    for origin in ("http://127.0.0.1:5173", "http://evil.test:15173", "https://127.0.0.1:15173",
                   "http://127.0.0.1:15174", "null"):
        resp = _approve(client, origin)
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "origin_forbidden", origin


def test_cli_rejects_bad_ui_port():
    from backend import api

    assert api.main(["--ui-port", "80"]) == 2
    assert api.main(["--ui-port", "70000"]) == 2


# ---- SANDBOX writes without SETTLEWELL_MOCK: real execute() gate, stateful stub remote, no network ----


def _sandbox_env(tmp_path, monkeypatch, remote, config=None, with_config=True):
    from test_api_p17 import SEEN, _boot, _ready, connect

    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    path = tmp_path / "sb.db"
    conn = connect(path)
    ob, inc = _ready(conn)
    from backend.api_proof import PROOF_BLOB

    conn.executescript(PROOF_BLOB)  # sandbox DBs are not seeded by init_db
    conn.close()
    kwargs = {"client_factory": lambda _c: remote, "clock": lambda: SEEN}
    if with_config:
        kwargs["mode_config"] = config or resolve_mode("sandbox", GOOD)
    client, headers = _boot(path, **kwargs)
    return client, headers, path, ob, inc


def _replay(client, headers, inc, intent_id):
    return client.post(f"/api/incidents/{inc}/proposals/{intent_id}/approve",
                       json={"binding_hash": "0" * 64}, headers=headers)


def test_sandbox_without_mock_env_executes_bound_replacement_once(tmp_path, monkeypatch):
    from decimal import Decimal

    from test_api_p17 import Remote, _open, _post, _sql

    remote = Remote()
    client, headers, path, ob, inc = _sandbox_env(tmp_path, monkeypatch, remote)
    health = client.get("/api/health").json()
    assert health["mode"] == "SANDBOX" and health["writes_enabled"] is True
    ref = _open(path, inc, ob)
    resp = _post(client, headers, inc, ref.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["kind"] == "replacement"
    assert len(remote.created) == 1 and remote.eur == Decimal("5800.00")
    assert _sql(path, "SELECT used_at FROM approval WHERE id = ?", ref.approval_id)[0][0]
    again = _replay(client, headers, inc, ref.id)
    assert again.status_code == 409 and again.json()["error"]["code"] == "approval_used"
    assert len(remote.created) == 1


def test_sandbox_without_mock_env_converts_locked_fx_once(tmp_path, monkeypatch):
    from decimal import Decimal

    from test_api_p17 import SEEN, Remote, _open, _post, connect, fund_replacement

    remote = Remote(eur="0.00")
    client, headers, path, ob, inc = _sandbox_env(tmp_path, monkeypatch, remote)
    conn = connect(path)
    locked = fund_replacement(conn, remote, obligation_id=ob, as_of=SEEN)
    conn.close()
    remote.buy_amount = locked.buy_amount
    ref = _open(path, inc, ob, kind="fx_conversion", action="FX_FUND", quote_id=locked.quote_id,
                amount=format(locked.buy_amount, "f"), failure_code=None)
    resp = _post(client, headers, inc, ref.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["kind"] == "fx_conversion"
    assert len(remote.conversions) == 1 and remote.eur == Decimal(locked.buy_amount)
    again = _replay(client, headers, inc, ref.id)
    assert again.status_code == 409 and again.json()["error"]["code"] == "approval_used"
    assert len(remote.conversions) == 1


def test_sandbox_foreign_origin_and_bad_csrf_denied(tmp_path, monkeypatch):
    from test_api_p17 import Remote, _open

    remote = Remote()
    client, headers, path, ob, inc = _sandbox_env(tmp_path, monkeypatch, remote)
    ref = _open(path, inc, ob)
    url = f"/api/incidents/{inc}/proposals/{ref.id}/approve"
    body = {"binding_hash": "0" * 64}
    foreign = client.post(url, json=body, headers={**headers, "Origin": "http://evil.test:8765"})
    assert foreign.status_code == 403 and foreign.json()["error"]["code"] == "origin_forbidden"
    assert client.post(url, json=body, headers={**headers, "X-Settlewell-CSRF": "nope"}).status_code == 403
    assert remote.created == []


@pytest.mark.parametrize("env", [{}, {CLIENT_ID_ENV: "x"}, {**GOOD, BASE_URL_ENV: "https://api.airwallex.com"}])
def test_read_only_never_writes_even_with_injected_client(tmp_path, monkeypatch, env):
    from test_api_p17 import Remote, _open, _post

    remote = Remote()
    config = resolve_mode("sandbox", env)
    assert config.mode == "READ_ONLY"
    client, headers, path, ob, inc = _sandbox_env(tmp_path, monkeypatch, remote, config)
    assert client.get("/api/session").json()["writes_enabled"] is False
    ref = _open(path, inc, ob)
    resp = _post(client, headers, inc, ref.id)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "writes_disabled"
    assert remote.created == []


def test_no_mode_config_and_no_mock_env_is_read_only_despite_client(tmp_path, monkeypatch):
    from test_api_p17 import Remote, _open, _post

    remote = Remote()
    client, headers, path, ob, inc = _sandbox_env(tmp_path, monkeypatch, remote, with_config=False)
    assert client.get("/api/session").json()["writes_enabled"] is False
    ref = _open(path, inc, ob)
    assert _post(client, headers, inc, ref.id).status_code == 503
    assert remote.created == []

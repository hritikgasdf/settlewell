"""Hosted demo: allowed hosts/origins, passcode gate, /health, static UI, reset. Offline."""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from backend import hosted
from backend.api import create_app
from backend.hosting import (
    COOKIE_NAME,
    Hosting,
    LoginLimiter,
    cookie_valid,
    host_of,
    make_cookie,
    passcode_ok,
)

PUBLIC = "https://settlewell-demo.up.railway.app"
PASS = "test-passcode-123"


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    for name in ("SETTLEWELL_ALLOWED_HOSTS", "SETTLEWELL_PUBLIC_ORIGIN", "SETTLEWELL_DEMO_PASSCODE",
                 "SETTLEWELL_STATIC_DIR", "RAILWAY_PUBLIC_DOMAIN", "SETTLEWELL_SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)


def _hosting(**env):
    return Hosting.from_env({"SETTLEWELL_PUBLIC_ORIGIN": PUBLIC, **env})


def _client(tmp_path, hosting, base=PUBLIC):
    return TestClient(create_app(str(tmp_path / "demo.db"), hosting=hosting), base_url=base)


def _login(client):
    r = client.post("/login", data={"passcode": PASS}, headers={"Origin": PUBLIC},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return r


# ---- unchanged local behaviour ----


def test_env_unset_is_loopback_only(tmp_path):
    hosting = Hosting.from_env({})
    assert hosting.allowed_hosts == frozenset() and not hosting.gate_enabled
    local = TestClient(create_app(str(tmp_path / "a.db"), hosting=hosting), base_url="http://127.0.0.1:8765")
    assert local.get("/api/incidents").status_code == 200
    remote = TestClient(create_app(str(tmp_path / "b.db"), hosting=hosting), base_url=PUBLIC)
    r = remote.get("/api/incidents")
    assert r.status_code == 403 and r.json()["error"] == {"code": "forbidden_host", "message": "loopback only"}
    assert remote.get("/login").status_code == 403  # no login route, host still refused


def test_health_ignores_host_for_platform_checks(tmp_path):
    client = _client(tmp_path, Hosting.from_env({}), base="http://healthcheck.railway.app")
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "mode": "MOCK"}


# ---- allowed hosts / origins ----


def test_public_origin_allows_its_host_and_origin(tmp_path):
    client = _client(tmp_path, _hosting())
    assert client.get("/api/incidents").status_code == 200
    token = client.get("/api/session").json()["csrf_token"]
    pid = client.get("/api/incidents/1").json()["proposals"][0]["id"]
    bad = client.post(f"/api/incidents/1/proposals/{pid}/reject",
                      json={"binding_hash": "0" * 64, "reason": "x"},
                      headers={"Origin": "https://evil.example", "X-Settlewell-CSRF": token})
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "origin_forbidden"
    no_csrf = client.post(f"/api/incidents/1/proposals/{pid}/reject",
                          json={"binding_hash": "0" * 64, "reason": "x"}, headers={"Origin": PUBLIC})
    assert no_csrf.json()["error"]["code"] == "csrf_missing"


def test_other_hosts_still_refused(tmp_path):
    client = _client(tmp_path, _hosting(), base="https://evil.example")
    assert client.get("/api/incidents").status_code == 403


def test_allowed_hosts_and_railway_domain():
    h = Hosting.from_env({"SETTLEWELL_ALLOWED_HOSTS": "Demo.Example.com, other.example",
                          "RAILWAY_PUBLIC_DOMAIN": "app.up.railway.app"})
    assert h.allowed_hosts == {"demo.example.com", "other.example", "app.up.railway.app"}
    assert h.public_origins == {"https://app.up.railway.app"}


@pytest.mark.parametrize("bad", ["https://x.example/path", "ftp://x.example", "https://u@x.example",
                                 "*", "not a url"])
def test_bad_origin_or_host_values_refused(bad):
    with pytest.raises(ValueError):
        Hosting.from_env({"SETTLEWELL_PUBLIC_ORIGIN": bad} if "://" in bad or bad == "not a url"
                         else {"SETTLEWELL_ALLOWED_HOSTS": bad})


def test_host_of():
    assert host_of("Example.com:443") == "example.com"
    assert host_of("[::1]:8765") == "[::1]"


def test_public_host_is_case_insensitive(tmp_path):
    """HTTP hosts are case-insensitive; mixed-case Host must still be allowed."""
    app = create_app(str(tmp_path / "demo.db"), hosting=_hosting())

    async def hit(host: str):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.invalid") as ac:
            return await ac.get("/api/incidents", headers={"host": host}, follow_redirects=False)

    ok = asyncio.run(hit("Settlewell-Demo.up.railway.app"))
    assert ok.status_code == 200
    assert ok.json()["incidents"][0]["invoice_ref"] == "INV-2291"
    bad = asyncio.run(hit("evil.example"))
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "forbidden_host"


# ---- passcode gate ----


def test_gate_blocks_api_and_pages_until_login(tmp_path):
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS))
    assert client.get("/health").status_code == 200
    r = client.get("/api/incidents")
    assert r.status_code == 401 and r.json()["error"]["code"] == "passcode_required"
    assert client.get("/api/session").status_code == 401
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert client.post("/api/demo/reset", headers={"Origin": PUBLIC}).status_code == 401
    form = client.get("/login")
    assert form.status_code == 200 and 'name="passcode"' in form.text and PASS not in form.text


def test_login_sets_strict_secure_httponly_cookie(tmp_path):
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS))
    r = _login(client)
    cookie = r.headers["set-cookie"].lower()
    for part in ("httponly", "secure", "samesite=strict", "path=/"):
        assert part in cookie
    assert PASS not in r.headers["set-cookie"]
    assert client.get("/api/incidents").status_code == 200
    assert client.get("/api/incidents/1").json()["supplier"]["name"] == "Lumen Textiles GmbH"


def test_wrong_passcode_and_rate_limit(tmp_path):
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS))
    for _ in range(5):
        r = client.post("/login", data={"passcode": "nope"}, headers={"Origin": PUBLIC})
        assert r.status_code == 401
    r = client.post("/login", data={"passcode": PASS}, headers={"Origin": PUBLIC})
    assert r.status_code == 429  # even the right passcode waits once the client is limited


def test_login_refuses_foreign_origin(tmp_path):
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS))
    r = client.post("/login", data={"passcode": PASS}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "origin_forbidden"
    # same-origin Referrer-Policy must not loosen the check: a null Origin is still refused.
    null = client.post("/login", data={"passcode": PASS}, headers={"Origin": "null"})
    assert null.status_code == 403 and null.json()["error"]["code"] == "origin_forbidden"


def test_login_page_sends_same_origin_referrer_policy(tmp_path):
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS))
    form = client.get("/login")
    assert form.status_code == 200
    assert form.headers["referrer-policy"] == "same-origin"
    # A same-origin Origin still reaches the passcode check (401, not 403).
    wrong = client.post("/login", data={"passcode": "nope"}, headers={"Origin": PUBLIC})
    assert wrong.status_code == 401


def test_forged_or_expired_cookie_refused(tmp_path):
    hosting = _hosting(SETTLEWELL_DEMO_PASSCODE=PASS)
    client = _client(tmp_path, hosting)
    other = make_cookie(b"another-secret")
    client.cookies.set(COOKIE_NAME, other)
    assert client.get("/api/incidents").status_code == 401
    assert cookie_valid(hosting.session_secret, make_cookie(hosting.session_secret))
    assert not cookie_valid(hosting.session_secret, make_cookie(hosting.session_secret, now=0))
    assert not cookie_valid(hosting.session_secret, "1.2")


def test_webhook_path_is_not_behind_the_gate(tmp_path):
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS))
    r = client.post("/api/webhooks/airwallex", content=b"{}")
    assert r.json()["error"]["code"] != "passcode_required"  # its own HMAC check answers


@pytest.mark.parametrize("path,method", [
    ("/", "POST"), ("/index.html", "GET"), ("/assets/app.js", "GET"),
    ("/api/incidents", "GET"), ("/api/incidents/", "GET"), ("/api/session", "OPTIONS"),
    ("/api/demo/reset", "POST"), ("/api/demo/status", "GET"),
    ("/health/", "GET"), ("/login/", "POST"),
    ("/health/../api/incidents", "GET"), ("/login/../api/incidents", "GET"),
    ("//api/incidents", "GET"), ("/openapi.json", "GET"),
])
def test_gate_holds_for_path_tricks_and_methods(tmp_path, path, method):
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS))
    r = client.request(method, path, headers={"Origin": PUBLIC}, follow_redirects=False)
    assert r.status_code in (401, 303)
    if r.status_code == 401:
        assert r.json()["error"]["code"] == "passcode_required"
    else:
        assert r.headers["location"] == "/login"
    assert PASS not in (r.text or "")


def test_passcode_compare_and_limiter():
    assert passcode_ok("abc", "abc") and not passcode_ok("abc", "abd") and not passcode_ok("", "")
    t = [0.0]
    lim = LoginLimiter(per_client=2, global_cap=3, window=10, clock=lambda: t[0])
    lim.fail("a")
    lim.fail("a")
    assert lim.blocked("a") and not lim.blocked("b")
    lim.fail("b")
    assert lim.blocked("c")  # global cap
    t[0] = 11
    assert not lim.blocked("a")


# ---- static UI and reset ----


def test_static_ui_served_behind_gate(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html>settlewell-ui</html>")
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS, SETTLEWELL_STATIC_DIR=str(ui)))
    assert client.get("/", follow_redirects=False).status_code == 303
    _login(client)
    page = client.get("/")
    assert page.status_code == 200 and "settlewell-ui" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "same-origin"
    assert client.get("/api/health").json()["mode"] == "MOCK"  # API routes win over the mount


def test_mock_reset_reseeds_through_guard(tmp_path):
    client = _client(tmp_path, _hosting(SETTLEWELL_DEMO_PASSCODE=PASS))
    _login(client)
    token = client.get("/api/session").json()["csrf_token"]
    headers = {"Origin": PUBLIC, "X-Settlewell-CSRF": token}
    assert client.post("/api/demo/reset", headers={"Origin": PUBLIC}).status_code == 403  # CSRF kept
    pid = client.get("/api/incidents/1").json()["proposals"][0]
    ok = client.post(f"/api/incidents/1/proposals/{pid['id']}/approve",
                     json={"binding_hash": pid["binding_hash"]}, headers=headers)
    assert ok.status_code == 200
    assert client.get("/api/incidents").json()["incidents"][0]["pending_proposals"] == 0
    r = client.post("/api/demo/reset", headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "done"
    rows = client.get("/api/incidents").json()["incidents"]
    assert len(rows) == 1 and rows[0]["invoice_ref"] == "INV-2291" and rows[0]["pending_proposals"] == 1
    conn = sqlite3.connect(tmp_path / "demo.db")
    try:
        assert conn.execute("SELECT status FROM mock_remote WHERE airwallex_id='synthetic-dup-2291'").fetchone()[0] == "SCHEDULED"
    finally:
        conn.close()


# ---- hosted entrypoint ----


def test_build_mock_defaults(tmp_path, monkeypatch):
    ui = tmp_path / "dist"
    ui.mkdir()
    (ui / "index.html").write_text("<html>ok</html>")
    env = {"SETTLEWELL_MODE": "mock", "PORT": "8080", "SETTLEWELL_DB": str(tmp_path / "h.db"),
           "SETTLEWELL_DEMO_PASSCODE": PASS, "SETTLEWELL_PUBLIC_ORIGIN": PUBLIC,
           "SETTLEWELL_STATIC_DIR": str(ui)}
    app, port = hosted.build(env)
    assert port == 8080
    client = TestClient(app, base_url=PUBLIC)
    assert client.get("/health").json()["mode"] == "MOCK"
    assert client.get("/api/incidents").status_code == 401


def test_build_refuses_sandbox_without_passcode(tmp_path):
    with pytest.raises(SystemExit):
        hosted.build({"SETTLEWELL_MODE": "sandbox", "SETTLEWELL_DB": str(tmp_path / "s.db")})
    with pytest.raises(SystemExit):
        hosted.build({"SETTLEWELL_MODE": "live"})


def test_build_sandbox_without_credentials_is_read_only(tmp_path, monkeypatch):
    for name in ("AIRWALLEX_SANDBOX_CLIENT_ID", "AIRWALLEX_SANDBOX_API_KEY", "AIRWALLEX_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    app, _ = hosted.build({"SETTLEWELL_MODE": "sandbox", "SETTLEWELL_DB": str(tmp_path / "s.db"),
                           "SETTLEWELL_DEMO_PASSCODE": PASS, "SETTLEWELL_PUBLIC_ORIGIN": PUBLIC})
    client = TestClient(app, base_url=PUBLIC)
    assert client.get("/health").json()["mode"] == "READ_ONLY"
    _login(client)
    body = client.get("/api/incidents").json()
    assert body.get("incidents") == [], body  # empty schema, no 500


def test_sandbox_demo_runner_serializes_and_records(tmp_path):
    calls = []
    manifest = tmp_path / "m.json"
    manifest.write_text("{}")
    demo = hosted.SandboxDemo(str(manifest), str(tmp_path / "s.db"),
                              runner=lambda: calls.append("run") or {"original_sent": "PASS"},
                              resetter=lambda: (calls.append("reset") or (True, [])))
    assert demo.start(reset=True) == (True, "")
    demo.join(5)
    assert calls == ["reset", "run"] and demo.status()["state"] == "done"
    ok, reason = demo.start(reset=True)
    assert not ok and "wait" in reason  # rerun interval


def test_sandbox_demo_reset_refusal_stops_rerun(tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text("{}")
    ran = []
    demo = hosted.SandboxDemo(str(manifest), str(tmp_path / "s.db"),
                              runner=lambda: ran.append(1) or {},
                              resetter=lambda: (False, ["tid:SENT"]))
    demo.start(reset=True)
    demo.join(5)
    status = demo.status()
    assert ran == [] and status["state"] == "failed" and "tid:SENT" in status["problems"]

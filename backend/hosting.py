"""Hosted-demo settings: allowed hosts/origins, the demo passcode gate and the built UI.

Every setting comes from environment variables. With none set, the API behaves exactly as the
local loopback demo: loopback Host only, loopback Origins only, no passcode, no static UI.
Secret values (the passcode, the session secret) are never logged or echoed.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

COOKIE_NAME = "settlewell_demo"
COOKIE_TTL_S = 12 * 3600
HEALTH_PATH = "/health"
LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
WEBHOOK_PATH = "/api/webhooks/airwallex"  # HMAC-signed by Airwallex; cannot log in
# Exempt from the passcode gate. Each one is read-only or has its own authentication.
GATE_EXEMPT = frozenset({HEALTH_PATH, LOGIN_PATH, WEBHOOK_PATH})
FAIL_WINDOW_S = 300
FAILS_PER_CLIENT = 5
FAILS_GLOBAL = 30
MAX_LOGIN_BODY = 2048


def _split(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _origin_of(value: str) -> tuple[str, str] | None:
    """(origin, host) for an exact http(s)://host[:port] value, else None."""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if "@" in parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    host = parsed.hostname.lower()
    origin = f"{parsed.scheme}://{host}" + (f":{port}" if port else "")
    return origin, host


def host_of(raw: str) -> str:
    """Hostname part of a Host header, lower-cased, without the port. IPv6 keeps brackets."""
    raw = (raw or "").strip().lower()
    if raw.startswith("[") and "]" in raw:
        return raw[: raw.index("]") + 1]
    return raw.split(":")[0]


@dataclass
class Hosting:
    allowed_hosts: frozenset[str] = frozenset()
    public_origins: frozenset[str] = frozenset()
    passcode: str = field(default="", repr=False)
    session_secret: bytes = field(default=b"", repr=False)
    static_dir: str | None = None

    @property
    def gate_enabled(self) -> bool:
        return bool(self.passcode)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, static_dir: str | None = None) -> Hosting:
        source = os.environ if env is None else env
        hosts = {h.lower() for h in _split(source.get("SETTLEWELL_ALLOWED_HOSTS"))}
        origins: set[str] = set()
        public = list(_split(source.get("SETTLEWELL_PUBLIC_ORIGIN")))
        railway = (source.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
        if railway:
            public.append(f"https://{railway}")
        for value in public:
            parsed = _origin_of(value)
            if parsed is None:
                raise ValueError("SETTLEWELL_PUBLIC_ORIGIN must be http(s)://host[:port] values")
            origins.add(parsed[0])
            hosts.add(parsed[1])
        for host in hosts:
            if not host or any(c in host for c in "/@ \t") or host == "*":
                raise ValueError("SETTLEWELL_ALLOWED_HOSTS must be plain host names")
        passcode = source.get("SETTLEWELL_DEMO_PASSCODE") or ""
        raw_secret = source.get("SETTLEWELL_SESSION_SECRET") or ""
        secret = raw_secret.encode() if raw_secret else secrets.token_bytes(32)
        return cls(frozenset(hosts), frozenset(origins), passcode, secret,
                   static_dir or source.get("SETTLEWELL_STATIC_DIR") or None)


# ---- signed session cookie ----


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def make_cookie(secret: bytes, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + COOKIE_TTL_S)
    nonce = secrets.token_hex(8)
    payload = f"{expires}.{nonce}"
    return f"{payload}.{_sign(secret, payload)}"


def cookie_valid(secret: bytes, value: str | None, now: float | None = None) -> bool:
    if not value or value.count(".") != 2:
        return False
    expires, nonce, sig = value.split(".")
    if not expires.isdigit():
        return False
    if not hmac.compare_digest(sig, _sign(secret, f"{expires}.{nonce}")):
        return False
    return int(expires) > (now if now is not None else time.time())


def passcode_ok(expected: str, supplied: str) -> bool:
    """Constant-time compare of digests, so length differences do not leak either."""
    a = hashlib.sha256(expected.encode()).digest()
    b = hashlib.sha256((supplied or "").encode()).digest()
    return bool(expected) and hmac.compare_digest(a, b)


class LoginLimiter:
    """Failed-attempt limiter: per client and global, over a sliding window."""

    def __init__(self, per_client: int = FAILS_PER_CLIENT, global_cap: int = FAILS_GLOBAL,
                 window: float = FAIL_WINDOW_S, clock=time.monotonic) -> None:
        self.per_client, self.global_cap, self.window, self.clock = per_client, global_cap, window, clock
        self._fails: dict[str, deque] = {}
        self._all: deque = deque()
        self._lock = threading.Lock()

    def _trim(self, q: deque, now: float) -> None:
        while q and now - q[0] > self.window:
            q.popleft()

    def blocked(self, client: str) -> bool:
        with self._lock:
            now = self.clock()
            q = self._fails.setdefault(client, deque())
            self._trim(q, now)
            self._trim(self._all, now)
            return len(q) >= self.per_client or len(self._all) >= self.global_cap

    def fail(self, client: str) -> None:
        with self._lock:
            now = self.clock()
            self._fails.setdefault(client, deque()).append(now)
            self._all.append(now)
            if len(self._fails) > 10_000:  # bound memory
                self._fails.clear()


def client_key(request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    return request.client.host if request.client else "unknown"


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def login_page(error: str = "") -> str:
    message = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settlewell demo - sign in</title>
<style>body{{font-family:system-ui,sans-serif;background:#f5f6f8;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0}}form{{background:#fff;padding:2rem;border-radius:12px;
box-shadow:0 2px 12px #0001;width:320px}}h1{{font-size:1.25rem;margin:0 0 .5rem}}p{{color:#555;
font-size:.9rem}}input{{width:100%;box-sizing:border-box;padding:.6rem;margin:.5rem 0 1rem;font-size:1rem}}
button{{width:100%;padding:.6rem;font-size:1rem;background:#1f3b73;color:#fff;border:0;border-radius:6px;
cursor:pointer}}.err{{color:#b00020}}</style></head>
<body><form method="post" action="{LOGIN_PATH}">
<h1>Settlewell demo</h1><p>Enter the demo passcode you were given.</p>{message}
<label for="passcode">Passcode</label>
<input id="passcode" name="passcode" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Open console</button></form></body></html>"""


def resolve_static_dir(candidate: str | None) -> Path | None:
    if not candidate:
        return None
    path = Path(candidate).resolve()
    return path if (path / "index.html").is_file() else None

"""Mode selection for the local demo API: MOCK (default), SANDBOX, or READ_ONLY.

SANDBOX needs the explicit flag, the exact Airwallex sandbox base URL, and both
credential variables. Anything else falls back to READ_ONLY (writes off). The
production host can never be selected. Credential values are never read into
messages; only variable names are reported.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass

SANDBOX_BASE_URL = "https://api-demo.airwallex.com"
CLIENT_ID_ENV = "AIRWALLEX_SANDBOX_CLIENT_ID"
API_KEY_ENV = "AIRWALLEX_SANDBOX_API_KEY"
BASE_URL_ENV = "AIRWALLEX_BASE_URL"
MODES = ("mock", "sandbox")


@dataclass(frozen=True)
class ModeConfig:
    mode: str  # "MOCK" | "SANDBOX" | "READ_ONLY"
    writes_enabled: bool
    reason: str = ""
    missing: tuple[str, ...] = ()
    requested: str = "mock"

    @property
    def banner(self) -> str:
        if self.mode == "SANDBOX":
            return "SANDBOX - virtual Airwallex sandbox funds, not production"
        if self.mode == "MOCK":
            return "MOCK / LOCAL DEMO - synthetic data, no live money"
        return f"READ ONLY - writes disabled ({self.reason})"


def exact_sandbox_url(value: str) -> bool:
    """True only for https://api-demo.airwallex.com, nothing else (no path, port, userinfo)."""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api-demo.airwallex.com"
        and port is None
        and "@" not in parsed.netloc
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


def resolve_mode(requested: str = "mock", env: Mapping[str, str] | None = None) -> ModeConfig:
    source = os.environ if env is None else env
    if requested not in MODES:
        return ModeConfig("READ_ONLY", False, "unknown mode", requested=str(requested))
    if requested == "mock":
        return ModeConfig("MOCK", True, requested="mock")
    base = source.get(BASE_URL_ENV, SANDBOX_BASE_URL)
    if not exact_sandbox_url(base):
        return ModeConfig("READ_ONLY", False, "base url is not the exact sandbox url", requested="sandbox")
    missing = tuple(n for n in (CLIENT_ID_ENV, API_KEY_ENV) if not source.get(n))
    if missing:
        return ModeConfig("READ_ONLY", False, "incomplete sandbox credentials", missing, "sandbox")
    return ModeConfig("SANDBOX", True, requested="sandbox")


def make_client_factory(config: ModeConfig, env: Mapping[str, str] | None = None,
                        transport=None) -> Callable:
    """Return factory(conn) -> client. SANDBOX uses the typed adapter pinned to the sandbox URL."""
    if config.mode == "MOCK":
        from backend.api import LedgerMockClient

        return LedgerMockClient
    if config.mode != "SANDBOX":
        return _read_only_factory
    from backend.airwallex.client import AirwallexClient

    source = os.environ if env is None else env
    shared = AirwallexClient(
        client_id=source[CLIENT_ID_ENV], api_key=source[API_KEY_ENV],
        base_url=SANDBOX_BASE_URL, transport=transport,
    )
    return lambda _conn: shared  # one instance: token cache and rate limiter are shared


def _read_only_factory(_conn):
    raise PermissionError("READ_ONLY: no remote client is available")

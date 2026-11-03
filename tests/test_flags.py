"""Fast checks that need no secrets and no network."""

from __future__ import annotations

import socket

import pytest

from settlewell.flags import (
    LOGIN_PATH,
    PINNED_API_VERSION,
    flat_failure_reason_persists,
    mock_mode,
    sandbox_enabled,
)


def test_login_path_and_pinned_version() -> None:
    assert LOGIN_PATH == "/api/v1/authentication/login"
    assert PINNED_API_VERSION == "2025-11-11"


def test_failure_reason_persists_only_with_pinned_version_header() -> None:
    assert flat_failure_reason_persists(None) is False
    assert flat_failure_reason_persists("") is False
    assert flat_failure_reason_persists("2025-11-11") is True


def test_flags_need_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    monkeypatch.delenv("SETTLEWELL_SANDBOX", raising=False)
    monkeypatch.delenv("AIRWALLEX_SANDBOX_CLIENT_ID", raising=False)
    monkeypatch.delenv("AIRWALLEX_SANDBOX_API_KEY", raising=False)
    assert mock_mode() is False
    assert sandbox_enabled() is False
    assert flat_failure_reason_persists(PINNED_API_VERSION) is True


def test_mock_mode_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    assert mock_mode() is True


def test_unit_suite_blocks_network() -> None:
    with pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("api-demo.airwallex.com", 443), timeout=1)

"""The local console starts in mock mode with no credentials. No network."""

from settlewell.__main__ import handle, local_mode, main


def test_the_default_run_is_mock_and_does_not_print_secrets(monkeypatch, capsys):
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    monkeypatch.delenv("SETTLEWELL_SANDBOX", raising=False)
    monkeypatch.setenv("AIRWALLEX_SANDBOX_API_KEY", "super-secret-key")
    monkeypatch.setenv("AIRWALLEX_SANDBOX_CLIENT_ID", "client-id")
    assert local_mode() == "mock"
    assert main(["--check"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "MOCK"
    assert "super-secret-key" not in out
    assert "client-id" not in out


def test_sandbox_mode_is_opt_in_and_does_not_call_airwallex(monkeypatch, capsys):
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    monkeypatch.setenv("SETTLEWELL_SANDBOX", "1")
    assert local_mode() == "sandbox"
    assert main(["--check"]) == 0
    assert capsys.readouterr().out.strip() == "sandbox"
    status, body = handle("/")
    text = body.decode()
    assert status == 200
    assert "does not call Airwallex" in text
    assert "Late SENT" not in text


def test_mock_console_serves_the_inbox(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    monkeypatch.delenv("SETTLEWELL_SANDBOX", raising=False)
    status, body = handle("/")
    page = body.decode()
    assert status == 200
    assert "Late SENT" in page
    assert 'href="/ledger"' in page
    assert 'href="/outbox"' in page
    assert "super-secret" not in page

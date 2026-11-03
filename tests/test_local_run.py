"""The local console starts in mock mode with no credentials. No network."""

from backend.airwallex.client import AirwallexClient
from backend.ledger import connect, seed_hero
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


def test_sandbox_mode_reads_the_ledger_and_does_not_call_airwallex(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    monkeypatch.setenv("SETTLEWELL_SANDBOX", "1")
    db = tmp_path / "ledger.db"
    monkeypatch.setenv("SETTLEWELL_DB", str(db))
    conn = connect(db)
    seed_hero(conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM obligation").fetchone()["n"]
    conn.close()
    assert local_mode() == "sandbox"
    assert main(["--check"]) == 0
    assert capsys.readouterr().out.strip() == "sandbox"

    def boom(self, *_args, **_kwargs):
        raise AssertionError("a page handler constructed an Airwallex client")

    monkeypatch.setattr(AirwallexClient, "__init__", boom)
    status, body = handle("/")
    text = body.decode()
    assert status == 200
    assert "does not call Airwallex" in text
    assert "INV-2291" in text
    assert "Late SENT" not in text
    incident_status, incident = handle("/incidents/1")
    assert incident_status == 200
    assert "No decision yet." in incident.decode()
    stored = connect(db)
    assert stored.execute("SELECT COUNT(*) AS n FROM obligation").fetchone()["n"] == before


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

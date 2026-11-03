"""Reset restores the hero and will not call a non-demo host. No network."""

from decimal import Decimal

import pytest

from backend.airwallex.types import Transfer
from backend.ledger import connect, seed_hero
from backend.reset import ResetError, reset_demo, sent_sandbox_ids
from backend.sandbox import SandboxDriver
from backend.scripts.reset_demo import main

DEMO = "https://api-demo.airwallex.com"


class Api:
    def __init__(self, status: str) -> None:
        self.status = status
        self.transitions: list[str] = []
        self.created = 0
        self.cancelled = 0

    def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer(id=transfer_id, status=self.status, transfer_amount=Decimal(1), transfer_currency="EUR")

    def post_json(self, path: str, body: dict) -> dict:
        assert "/simulation/transfers/" in path
        self.transitions.append(body["next_status"])
        self.status = body["next_status"]
        return {"id": "tr_live", "status": self.status}

    def create_transfer(self, **_kwargs):
        self.created += 1
        raise AssertionError("reset does not create transfers")

    def cancel_transfer(self, _transfer_id: str):
        self.cancelled += 1
        raise AssertionError("reset does not cancel through the transfer API")


def test_reset_refuses_a_non_demo_base():
    conn = connect()
    with pytest.raises(ResetError) as caught:
        reset_demo(conn, base_url="https://api.airwallex.com")
    assert caught.value.code == "base"


def test_release_sent_is_idempotent_and_uses_simulation_only():
    api = Api("SENT")
    driver = SandboxDriver(api, sleep=lambda _seconds: None)
    first = driver.release_sent("tr_live")
    second = driver.release_sent("tr_live")
    assert first.status == "PAID"
    assert second.status == "PAID"
    assert api.transitions == ["PAID"]
    assert api.created == 0
    assert api.cancelled == 0


def test_reset_restores_the_hero_and_releases_only_real_sent_ids():
    conn = connect()
    seed_hero(conn)
    conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, origin, amount, currency, status, reference
        ) VALUES ('tr_live', 'stl-live-1', 'settlewell', '10.00', 'EUR', 'SENT', 'INV-LIVE')
        """
    )
    conn.execute(
        "INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status) VALUES ('INV-OTHER', '1.00', 'EUR', 1, 'OPEN')"
    )
    conn.commit()
    released: list[str] = []

    class Driver:
        def release_sent(self, transfer_id: str) -> None:
            released.append(transfer_id)

    reset_demo(conn, base_url=DEMO, driver=Driver())
    reset_demo(conn, base_url=DEMO, driver=Driver())
    assert released == ["tr_live"]
    assert sent_sandbox_ids(conn) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM obligation").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM incident").fetchone()["n"] == 1
    hero = conn.execute(
        "SELECT status, airwallex_id FROM transfer_record"
    ).fetchone()
    assert hero["airwallex_id"].startswith("synthetic")
    assert conn.execute(
        "SELECT id FROM transfer_record WHERE status = 'SENT' AND airwallex_id NOT LIKE 'synthetic%'"
    ).fetchone() is None


def test_reset_command_does_not_print_credentials(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("AIRWALLEX_SANDBOX_API_KEY", "super-secret-key")
    monkeypatch.setenv("AIRWALLEX_SANDBOX_CLIENT_ID", "client-id")
    monkeypatch.delenv("SETTLEWELL_SANDBOX", raising=False)
    monkeypatch.delenv("AIRWALLEX_BASE_URL", raising=False)
    code = main(["--db", str(tmp_path / "ledger.db")])
    out = capsys.readouterr().out
    assert code == 0
    assert "super-secret-key" not in out
    assert "client-id" not in out
    assert "reset_supplier pass" in out
    assert "reset_sent pass" in out
    again = main(["--db", str(tmp_path / "ledger.db")])
    assert again == 0

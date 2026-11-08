"""Reset restores the hero and will not call a non-demo host. No network."""

from decimal import Decimal

import pytest

from backend.airwallex.types import Transfer, TransferPage
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


def test_reset_on_a_fresh_database_migrates_before_listing():
    conn = connect()

    class Driver:
        def list_remote_sent(self) -> list[str]:
            return []

        def release_sent(self, transfer_id: str) -> None:
            raise AssertionError(transfer_id)

    reset_demo(conn, base_url=DEMO, driver=Driver())
    assert conn.execute("SELECT invoice_ref FROM obligation").fetchone()["invoice_ref"] == "INV-2291"


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


def test_remote_sent_listing_stops_after_five_pages():
    class Pages:
        def __init__(self) -> None:
            self.calls = 0

        def list_transfers(self, *, request_id=None, short_reference_id=None, page_size=None, page=None):
            del request_id, short_reference_id, page
            assert page_size == 10
            self.calls += 1
            return TransferPage(
                items=(
                    Transfer(id=f"tr_page_{self.calls}", status="SENT"),
                    Transfer(id="synthetic-remote", status="SENT"),
                    Transfer(id=f"tr_paid_{self.calls}", status="PAID"),
                ),
                page_after="more",
            )

    api = Pages()
    ids = SandboxDriver(api, sleep=lambda _seconds: None).list_remote_sent()
    assert api.calls == 5
    assert ids == [f"tr_page_{number}" for number in range(1, 6)]


def test_reset_releases_a_remote_sent_transfer_that_is_not_in_the_ledger():
    conn = connect()
    seed_hero(conn)
    released: list[str] = []

    class Driver:
        def list_remote_sent(self) -> list[str]:
            return ["tr_remote", "synthetic-remote", "tr_remote"]

        def release_sent(self, transfer_id: str) -> None:
            released.append(transfer_id)

    reset_demo(conn, base_url=DEMO, driver=Driver())
    assert released == ["tr_remote"]


def test_reset_command_does_not_print_credentials(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("AIRWALLEX_SANDBOX_API_KEY", "super-secret-key")
    monkeypatch.setenv("AIRWALLEX_SANDBOX_CLIENT_ID", "client-id")
    monkeypatch.delenv("SETTLEWELL_SANDBOX", raising=False)
    monkeypatch.delenv("AIRWALLEX_BASE_URL", raising=False)
    manifest = str(tmp_path / "manifest.json")
    code = main(["--db", str(tmp_path / "ledger.db"), "--manifest", manifest])
    out = capsys.readouterr().out
    assert code == 0
    assert "super-secret-key" not in out
    assert "client-id" not in out
    assert "reset mock: manifest removed" in out
    again = main(["--db", str(tmp_path / "ledger.db"), "--manifest", manifest])
    assert again == 0

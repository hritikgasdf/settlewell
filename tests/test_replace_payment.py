"""Concurrent replace creates one transfer. A timeout is resolved by request id."""

import threading
from decimal import Decimal

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import Transfer, TransferPage
from backend.ledger import connect, seed_hero
from backend.replace_payment import replace_payment, replacement_request_id


def _transfer(transfer_id: str, request_id: str) -> Transfer:
    return Transfer(
        id=transfer_id,
        status="SCHEDULED",
        request_id=request_id,
        transfer_amount=Decimal("4200.00"),
        transfer_currency="EUR",
        reference="INV-2291",
    )


class Api:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.mode = "ok"
        self.found = _transfer("awx-existing", "stl-inv2291-2")

    def create_transfer(self, **kwargs):
        request_id = kwargs["request_id"]
        self.created.append(request_id)
        if self.mode == "duplicate":
            raise AirwallexError(400, "duplicate_request_id", "")
        if self.mode == "timeout":
            raise TimeoutError
        return _transfer("awx-new", request_id)

    def list_transfers(self, **kwargs):
        if kwargs.get("request_id") == self.found.request_id:
            return TransferPage(items=(self.found,), page_after=None)
        return TransferPage(items=(), page_after=None)


def _obligation(conn):
    seed_hero(conn)
    conn.execute("UPDATE transfer_record SET status = 'CANCELLED'")
    conn.commit()
    return conn.execute("SELECT id FROM obligation").fetchone()["id"]


def test_request_id_is_deterministic_and_within_bounds():
    request_id = replacement_request_id("INV-2291", 2)
    assert request_id == "stl-inv2291-2"
    assert 10 <= len(request_id) <= 50


def test_duplicate_request_id_returns_the_transfer_from_the_lookup():
    conn = connect()
    obligation_id = _obligation(conn)
    api = Api()
    api.mode = "duplicate"
    airwallex_id = replace_payment(
        conn,
        api,
        obligation_id=obligation_id,
        attempt=2,
        beneficiary_id="ben_1",
        details_hash="onboarding-v1",
        transfer_method="SWIFT",
        reason="goods",
    )
    assert airwallex_id == "awx-existing"
    assert api.created == ["stl-inv2291-2"]
    rows = conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-2'"
    ).fetchall()
    assert [row["airwallex_id"] for row in rows] == ["awx-existing"]


def test_a_timeout_is_resolved_by_looking_up_the_request_id():
    conn = connect()
    obligation_id = _obligation(conn)
    api = Api()
    api.mode = "timeout"
    airwallex_id = replace_payment(
        conn,
        api,
        obligation_id=obligation_id,
        attempt=2,
        beneficiary_id="ben_1",
        details_hash="onboarding-v1",
        transfer_method="SWIFT",
        reason="goods",
    )
    assert airwallex_id == "awx-existing"
    assert api.created == ["stl-inv2291-2"]


def test_concurrent_replace_makes_one_transfer(tmp_path):
    path = tmp_path / "ledger.db"
    setup = connect(path)
    obligation_id = _obligation(setup)
    setup.close()
    api = Api()
    barrier = threading.Barrier(2)
    found: list[str] = []

    def worker() -> None:
        conn = connect(path)
        try:
            barrier.wait()
            found.append(
                replace_payment(
                    conn,
                    api,
                    obligation_id=obligation_id,
                    attempt=2,
                    beneficiary_id="ben_1",
                    details_hash="onboarding-v1",
                    transfer_method="SWIFT",
                    reason="goods",
                )
            )
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert api.created == ["stl-inv2291-2"]
    assert found == ["awx-new", "awx-new"]
    check = connect(path)
    rows = check.execute(
        "SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-2'"
    ).fetchall()
    assert [row["airwallex_id"] for row in rows] == ["awx-new"]

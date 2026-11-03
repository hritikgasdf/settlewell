"""A colleague's scheduled duplicate becomes its own incident. No network."""

from decimal import Decimal

from backend.airwallex.types import Transfer, TransferPage
from backend.ledger import connect, seed_hero
from backend.paid_once import scan_paid_once


def _transfer(transfer_id: str, status: str, request_id: str, **overrides) -> Transfer:
    fields = {
        "id": transfer_id,
        "status": status,
        "request_id": request_id,
        "transfer_amount": Decimal("4200.00"),
        "transfer_currency": "EUR",
        "reference": "INV-2291",
    }
    fields.update(overrides)
    return Transfer(**fields)


class Account:
    def __init__(self, items: tuple[Transfer, ...]) -> None:
        self._items = items

    def list_transfers(self, **_kwargs):
        return TransferPage(items=self._items, page_after=None)


def test_a_scheduled_colleague_payment_is_its_own_incident():
    conn = connect()
    original = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (original,)).fetchone()[0]
    client = Account(
        (
            _transfer("awx-hero", "SENT", "stl-inv2291-1"),
            _transfer("awx-dup", "SCHEDULED", "colleague-friday"),
            _transfer("awx-other", "SCHEDULED", "other-invoice", reference="INV-9999"),
            _transfer("awx-dead", "CANCELLED", "colleague-old"),
        )
    )

    opened = scan_paid_once(conn, client, obligation_id)
    again = scan_paid_once(conn, client, obligation_id)

    assert len(opened) == 1
    assert again == []
    row = conn.execute("SELECT state, claim_json FROM incident WHERE id = ?", (opened[0],)).fetchone()
    assert row["state"] == "DUPLICATE"
    assert row["claim_json"] == "duplicate:awx-dup"
    assert conn.execute("SELECT COUNT(*) AS n FROM incident").fetchone()["n"] == 2
    external = conn.execute(
        "SELECT origin, request_id FROM transfer_record WHERE airwallex_id = 'awx-dup'"
    ).fetchone()
    assert external["origin"] == "external"
    assert external["request_id"] == "colleague-friday"

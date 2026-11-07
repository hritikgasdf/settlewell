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
    conn.execute(
        "UPDATE bank_detail_version SET beneficiary_id = 'ben_lumen' WHERE source = 'onboarding'"
    )
    conn.commit()
    client = Account(
        (
            _transfer("awx-hero", "SENT", "hero-inv2291", beneficiary_id="ben_lumen"),
            _transfer("awx-dup", "SCHEDULED", "colleague-friday", beneficiary_id="ben_lumen"),
            _transfer("awx-other", "SCHEDULED", "other-invoice", reference="INV-9999", beneficiary_id="ben_lumen"),
            _transfer("awx-stranger", "SCHEDULED", "other-supplier", beneficiary_id="ben_stranger"),
            _transfer("awx-dead", "CANCELLED", "colleague-old", beneficiary_id="ben_lumen"),
        )
    )

    opened = scan_paid_once(conn, client, obligation_id)
    again = scan_paid_once(conn, client, obligation_id)

    assert again == []
    states = {
        row["claim_json"]: row["state"]
        for row in conn.execute("SELECT state, claim_json FROM incident WHERE id != ?", (original,))
    }
    assert states == {"duplicate:awx-dup": "DUPLICATE", "ambiguous:awx-stranger": "AMBIGUOUS"}
    assert set(opened) == {
        row["id"]
        for row in conn.execute("SELECT id FROM incident WHERE id != ?", (original,))
    }
    assert conn.execute("SELECT COUNT(*) AS n FROM incident").fetchone()["n"] == 3
    external = conn.execute(
        "SELECT origin, request_id FROM transfer_record WHERE airwallex_id = 'awx-dup'"
    ).fetchone()
    assert external["origin"] == "external"
    assert external["request_id"] == "colleague-friday"


def test_the_scan_reads_past_250_transfers():
    conn = connect()
    original = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (original,)).fetchone()[0]
    conn.execute(
        "UPDATE bank_detail_version SET beneficiary_id = 'ben_lumen' WHERE source = 'onboarding'"
    )
    conn.commit()
    noise = [
        _transfer(f"noise-{index}", "PAID", f"noise-{index}", reference="OTHER", beneficiary_id="ben_other")
        for index in range(250)
    ]
    chunks = [noise[index : index + 50] for index in range(0, 250, 50)]
    chunks.append(
        [_transfer("awx-late", "SCHEDULED", "colleague-late", beneficiary_id="ben_lumen")]
    )

    class Paged:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def list_transfers(self, page=None, **_kwargs):
            index = 0 if page is None else int(page)
            self.calls.append(index)
            page_after = None if index + 1 >= len(chunks) else str(index + 1)
            return TransferPage(items=tuple(chunks[index]), page_after=page_after)

    client = Paged()
    scan_paid_once(conn, client, obligation_id)

    assert client.calls == [0, 1, 2, 3, 4, 5]
    row = conn.execute(
        "SELECT state, claim_json FROM incident WHERE claim_json = 'duplicate:awx-late'"
    ).fetchone()
    assert row["state"] == "DUPLICATE"

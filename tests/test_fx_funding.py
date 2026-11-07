"""EUR under the reserve floor is bought with USD, up to a coded limit. No network."""

from decimal import Decimal

import pytest

from backend.airwallex.types import Balance, Conversion, FxQuote, Transfer
from backend.fx_funding import (
    FX_LIMIT,
    RESERVE_FLOOR,
    FundingError,
    conversion_request_id,
    fund_replacement,
    reserve_floor,
)
from backend.ledger import connect, seed_hero

AS_OF = "2026-11-03T12:00:00+00:00"
FEE = Decimal("13.47")  # the original transfer's own fee, the labelled estimate


def _balance(currency: str, available: str) -> Balance:
    amount = Decimal(available)
    return Balance(
        currency=currency,
        available_amount=amount,
        pending_amount=Decimal(0),
        reserved_amount=Decimal(0),
        total_amount=amount,
        prepayment_amount=Decimal(0),
        account_type="cash",
    )


class Book:
    def __init__(self, eur: str, usd: str, original: str = "4200.00") -> None:
        self.original = original
        self.eur = Decimal(eur)
        self.usd = Decimal(usd)
        self.quotes: list[dict] = []
        self.conversions: list[dict] = []
        self.sell = Decimal(0)

    def get_transfer(self, transfer_id: str) -> Transfer:
        """The live original: its amount and own fee drive the plan, not the local obligation."""
        return Transfer(
            id=transfer_id, status="CANCELLED", request_id="original-payment",
            transfer_amount=Decimal(self.original), transfer_currency="EUR",
            fee_amount=FEE, fee_currency="EUR", reference="INV-2291",
        )

    def current_balances(self) -> list[Balance]:
        return [_balance("EUR", str(self.eur)), _balance("USD", str(self.usd))]

    def create_quote(self, **kwargs) -> FxQuote:
        self.quotes.append(kwargs)
        buy = kwargs["buy_amount"]
        self.sell = (buy * Decimal("1.10")).quantize(Decimal("0.01"))
        return FxQuote(
            quote_id="quo_hero",
            client_rate=Decimal("1.10"),
            buy_currency="EUR",
            sell_currency="USD",
            buy_amount=buy,
            sell_amount=self.sell,
            valid_to_at="2026-11-03T12:15:00+00:00",
            usage="MULTI_USE",
        )

    def create_conversion(self, **kwargs) -> Conversion:
        self.conversions.append(kwargs)
        self.eur += kwargs["buy_amount"]
        self.usd -= self.sell
        return Conversion(conversion_id="con_hero", status="SETTLED", quote_id=kwargs["quote_id"])


def _obligation(conn, *, amount: str | None = None) -> int:
    seed_hero(conn)
    if amount is not None:
        conn.execute("UPDATE obligation SET amount = ?", (amount,))
        conn.commit()
    return int(conn.execute("SELECT id FROM obligation").fetchone()["id"])


def test_eur_at_the_floor_does_not_convert():
    conn = connect()
    obligation_id = _obligation(conn)
    needed = Decimal("4200.00") + FEE + RESERVE_FLOOR
    book = Book(str(needed), "100000.00")

    result = fund_replacement(conn, book, obligation_id=obligation_id, as_of=AS_OF)

    assert result.action == "ready"
    assert book.quotes == []
    assert book.conversions == []
    labels = [row["label"] for row in conn.execute("SELECT label FROM balance_snapshot")]
    assert labels == ["before", "before"]


def test_eur_under_the_floor_converts_usd_within_the_limit():
    conn = connect()
    obligation_id = _obligation(conn)
    book = Book("800.00", "100000.00")
    shortfall = Decimal("4200.00") + FEE + RESERVE_FLOOR - Decimal("800.00")
    assert shortfall <= FX_LIMIT

    result = fund_replacement(conn, book, obligation_id=obligation_id, as_of=AS_OF)

    assert result.action == "locked"
    assert result.quote_id == "quo_hero"
    assert result.conversion_id is None
    assert result.buy_amount == shortfall
    assert book.conversions == []
    assert book.quotes == [
        {
            "buy_currency": "EUR",
            "sell_currency": "USD",
            "validity": "MIN_15",
            "buy_amount": shortfall,
        }
    ]
    quote = conn.execute("SELECT state, pair FROM fx_quote").fetchone()
    assert quote["state"] == "LOCKED"
    assert quote["pair"] == "USD/EUR"
    snapshots = conn.execute(
        "SELECT currency, available, label FROM balance_snapshot ORDER BY id"
    ).fetchall()
    assert [(row["currency"], row["label"]) for row in snapshots] == [
        ("EUR", "before"),
        ("USD", "before"),
    ]
    assert snapshots[0]["available"] == "800.00"


def test_a_top_up_over_the_limit_escalates_without_a_conversion():
    conn = connect()
    obligation_id = _obligation(conn, amount="15000.00")
    book = Book("800.00", "100000.00", original="15000.00")
    shortfall = Decimal("15000.00") + FEE + RESERVE_FLOOR - Decimal("800.00")
    assert shortfall > FX_LIMIT

    result = fund_replacement(conn, book, obligation_id=obligation_id, as_of=AS_OF)

    assert result.action == "escalate"
    assert result.buy_amount == shortfall
    assert book.quotes == []
    assert book.conversions == []
    assert conn.execute("SELECT id FROM fx_quote").fetchone() is None


def test_a_quote_the_usd_balance_cannot_cover_does_not_convert():
    conn = connect()
    obligation_id = _obligation(conn)
    book = Book("800.00", "1.00")

    result = fund_replacement(conn, book, obligation_id=obligation_id, as_of=AS_OF)

    assert result.action == "escalate"
    assert book.quotes
    assert book.conversions == []
    quote = conn.execute("SELECT state FROM fx_quote").fetchone()
    assert quote["state"] == "LOCKED"


def test_each_quote_gets_its_own_conversion_request_id():
    first = conversion_request_id("quo_one")
    second = conversion_request_id("quo_two")
    assert first != second
    assert first == conversion_request_id("quo_one")
    assert 10 <= len(first) <= 50
    assert 10 <= len(second) <= 50


@pytest.mark.parametrize("raw", ("nope", "0", "-1", "1,000"))
def test_a_reserve_floor_that_is_not_a_positive_number_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("SETTLEWELL_RESERVE_FLOOR", raw)
    with pytest.raises(FundingError) as caught:
        reserve_floor()
    assert caught.value.code == "floor"
    assert str(caught.value) == "SETTLEWELL_RESERVE_FLOOR must be a positive number"


def test_a_demo_floor_above_the_balance_converts_once_under_the_limit(monkeypatch):
    assert reserve_floor() == RESERVE_FLOOR
    conn = connect()
    obligation_id = _obligation(conn)
    book = Book("10000.00", "100000.00")
    monkeypatch.setenv("SETTLEWELL_RESERVE_FLOOR", "15000.00")
    shortfall = Decimal("4200.00") + FEE + Decimal("15000.00") - Decimal("10000.00")
    assert shortfall <= FX_LIMIT

    result = fund_replacement(conn, book, obligation_id=obligation_id, as_of=AS_OF)

    assert result.action == "locked"
    assert result.buy_amount == shortfall
    assert book.conversions == []
    stored = conn.execute("SELECT state FROM fx_quote").fetchone()
    assert stored["state"] == "LOCKED"

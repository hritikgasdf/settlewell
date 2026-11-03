"""Buy EUR with USD when a replacement would leave less than the reserve floor."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.ledger import consume_fx_quote, lock_fx_quote, record_fx_conversion

RESERVE_FLOOR = Decimal("1000.00")
FX_LIMIT = Decimal("10000.00")
_QUOTE_VALIDITY = "MIN_15"


class FundingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FundingResult:
    action: str
    quote_id: str | None = None
    conversion_id: str | None = None
    buy_amount: Decimal | None = None


def fund_replacement(conn, client, *, obligation_id: int, as_of: str) -> FundingResult:
    """Cover the EUR payment plus the floor. A top-up above the limit is not sent."""
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if obligation is None:
        raise FundingError("missing")
    if obligation["currency"] != "EUR":
        raise FundingError("currency")
    balances = client.current_balances()
    eur = _available(balances, "EUR")
    usd = _available(balances, "USD")
    _snapshot(conn, "EUR", eur, as_of, "before")
    _snapshot(conn, "USD", usd, as_of, "before")
    needed = Decimal(obligation["amount"]) + RESERVE_FLOOR
    if eur >= needed:
        conn.commit()
        return FundingResult("ready")
    shortfall = needed - eur
    if shortfall > FX_LIMIT:
        conn.commit()
        return FundingResult("escalate", buy_amount=shortfall)
    quote = client.create_quote(
        buy_currency="EUR",
        sell_currency="USD",
        validity=_QUOTE_VALIDITY,
        buy_amount=shortfall,
    )
    lock_fx_quote(
        conn,
        quote_id=quote.quote_id,
        pair="USD/EUR",
        rate=format(quote.client_rate if quote.client_rate is not None else Decimal(0), "f"),
        amount=format(shortfall, "f"),
        valid_to=quote.valid_to_at or as_of,
    )
    if quote.sell_amount is not None and quote.sell_amount > usd:
        conn.commit()
        return FundingResult("escalate", quote_id=quote.quote_id, buy_amount=shortfall)
    consume_fx_quote(conn, quote.quote_id, "")
    conn.commit()
    conversion = client.create_conversion(
        quote_id=quote.quote_id,
        buy_currency="EUR",
        sell_currency="USD",
        buy_amount=shortfall,
        request_id=f"stl-fx-{obligation_id}",
    )
    record_fx_conversion(conn, quote.quote_id, conversion.conversion_id)
    after = client.current_balances()
    _snapshot(conn, "EUR", _available(after, "EUR"), as_of, "after")
    _snapshot(conn, "USD", _available(after, "USD"), as_of, "after")
    conn.commit()
    return FundingResult(
        "converted",
        quote_id=quote.quote_id,
        conversion_id=conversion.conversion_id,
        buy_amount=shortfall,
    )


def _available(balances, currency: str) -> Decimal:
    for row in balances:
        if row.currency == currency:
            return row.available_amount
    return Decimal(0)


def _snapshot(conn, currency: str, available: Decimal, taken_at: str, label: str) -> None:
    conn.execute(
        """
        INSERT INTO balance_snapshot (currency, available, taken_at, label)
        VALUES (?, ?, ?, ?)
        """,
        (currency, format(available, "f"), taken_at, label),
    )

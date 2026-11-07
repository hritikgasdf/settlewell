"""Buy EUR with USD when a replacement would leave less than the reserve floor."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from backend.ledger import lock_fx_quote

RESERVE_FLOOR = Decimal("1000.00")
FX_LIMIT = Decimal("10000.00")
_QUOTE_VALIDITY = "MIN_15"
_FLOOR_ENV = "SETTLEWELL_RESERVE_FLOOR"


def conversion_request_id(quote_id: str) -> str:
    """One Airwallex request id per quote, so a later quote is not rejected as a replay."""
    slug = "".join(character for character in quote_id.lower() if character.isalnum())[-24:]
    request_id = f"stl-fx-{slug or 'quote'}"
    if len(request_id) < 10:
        request_id = f"{request_id}quote"[:10]
    return request_id[:50]


def reserve_floor() -> Decimal:
    """Default €1,000. A rehearsal may set a higher floor so the sandbox converts."""
    raw = os.environ.get(_FLOOR_ENV, "").strip()
    if not raw:
        return RESERVE_FLOOR
    try:
        floor = Decimal(raw)
    except InvalidOperation:
        raise FundingError(
            "floor",
            "SETTLEWELL_RESERVE_FLOOR must be a positive number",
        ) from None
    if not floor.is_finite() or floor <= 0:
        raise FundingError(
            "floor",
            "SETTLEWELL_RESERVE_FLOOR must be a positive number",
        )
    return floor


class FundingError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class FundingResult:
    action: str
    quote_id: str | None = None
    conversion_id: str | None = None
    buy_amount: Decimal | None = None


def fund_replacement(conn, client, *, obligation_id: int, as_of: str) -> FundingResult:
    """Plan EUR cover from the LIVE original transfer: amount + fee estimate + floor.

    Planning only: no conversion, no payout. The fee is the conservative fallback
    budget (`conservative_upper_bound_estimate`), not a bank quote. Anything missing or unknown raises FundingError.
    A top-up above the limit is not sent."""
    from backend.funding import _READ_ERRORS, _decimal, _fee_estimate

    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if obligation is None:
        raise FundingError("missing")
    if obligation["currency"] != "EUR":
        raise FundingError("currency")
    record = None
    if obligation["live_transfer_id"] is not None:
        record = conn.execute(
            "SELECT airwallex_id FROM transfer_record WHERE id = ?", (obligation["live_transfer_id"],)
        ).fetchone()
    if record is None or not record["airwallex_id"]:
        raise FundingError("original")
    try:
        remote = client.get_transfer(record["airwallex_id"])
    except _READ_ERRORS:
        raise FundingError("original", "original transfer unreadable") from None
    original = _decimal(remote.transfer_amount)
    if original is None or original <= 0:
        raise FundingError("original", "original amount unknown")
    if remote.transfer_currency != "EUR":
        raise FundingError("currency")
    estimate = _fee_estimate(remote, "EUR")
    if estimate is None:
        raise FundingError("fee", "original fee unknown")
    floor = reserve_floor()
    try:
        balances = client.current_balances()
    except _READ_ERRORS:
        raise FundingError("balance", "balance unreadable") from None
    eur = _available(balances, "EUR")
    usd = _available(balances, "USD")
    _snapshot(conn, "EUR", eur, as_of, "before")
    _snapshot(conn, "USD", usd, as_of, "before")
    needed = original + estimate[0] + floor
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
    conn.commit()
    return FundingResult("locked", quote_id=quote.quote_id, buy_amount=shortfall)


def _available(balances, currency: str) -> Decimal:
    """The one available amount. A missing, duplicate, or malformed row is unknown, never zero."""
    from backend.funding import cash_available

    try:
        return cash_available(balances, currency)
    except (ValueError, AttributeError, TypeError):
        raise FundingError("balance", f"{currency} cash available unknown") from None


def _snapshot(conn, currency: str, available: Decimal, taken_at: str, label: str) -> None:
    conn.execute(
        """
        INSERT INTO balance_snapshot (currency, available, taken_at, label)
        VALUES (?, ?, ?, ?)
        """,
        (currency, format(available, "f"), taken_at, label),
    )

"""Typed Airwallex results. Money is Decimal so fees such as 13.47 stay exact."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def money(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class Balance:
    currency: str
    available_amount: Decimal
    pending_amount: Decimal
    reserved_amount: Decimal
    total_amount: Decimal
    prepayment_amount: Decimal
    account_type: str | None = None


@dataclass(frozen=True)
class BalanceHistoryItem:
    id: str
    source: str | None
    amount: Decimal
    currency: str
    balance: Decimal | None
    description: str | None
    fee: Decimal | None
    posted_at: str | None
    source_type: str | None
    transaction_type: str | None
    account_type: str | None
    reference: str | None
    status: str | None = None  # as returned; None when the response carries none


@dataclass(frozen=True)
class BalanceHistoryPage:
    items: tuple[BalanceHistoryItem, ...]
    has_more: bool
    page_after: str | None


@dataclass(frozen=True)
class Transfer:
    id: str
    status: str
    request_id: str | None = None
    transfer_amount: Decimal | None = None
    transfer_currency: str | None = None
    source_amount: Decimal | None = None
    source_currency: str | None = None
    fee_amount: Decimal | None = None
    fee_currency: str | None = None
    short_reference_id: str | None = None
    reference: str | None = None
    beneficiary_id: str | None = None
    transfer_date: str | None = None
    dispatch_date: str | None = None
    transfer_method: str | None = None
    failure_reason: str | None = None
    failure_type: str | None = None


@dataclass(frozen=True)
class TransferPage:
    items: tuple[Transfer, ...]
    page_after: str | None


@dataclass(frozen=True)
class CancelResult:
    ok: bool
    transfer: Transfer | None = None


@dataclass(frozen=True)
class BeneficiaryValidation:
    ok: bool
    field_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Beneficiary:
    id: str
    nickname: str | None = None


@dataclass(frozen=True)
class ConfirmationLetter:
    content: bytes
    media_type: str


@dataclass(frozen=True)
class FxQuote:
    quote_id: str
    client_rate: Decimal | None
    buy_currency: str | None
    sell_currency: str | None
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    valid_to_at: str | None
    usage: str | None


@dataclass(frozen=True)
class Conversion:
    conversion_id: str
    status: str | None
    quote_id: str | None


def parse_balance(data: dict) -> Balance:
    return Balance(
        currency=str(data["currency"]),
        available_amount=money(data.get("available_amount")) or Decimal(0),
        pending_amount=money(data.get("pending_amount")) or Decimal(0),
        reserved_amount=money(data.get("reserved_amount")) or Decimal(0),
        total_amount=money(data.get("total_amount")) or Decimal(0),
        prepayment_amount=money(data.get("prepayment_amount")) or Decimal(0),
        account_type=_str(data.get("account_type")),
    )


def parse_history_item(data: dict) -> BalanceHistoryItem:
    return BalanceHistoryItem(
        id=str(data["id"]),
        source=_str(data.get("source")),
        amount=money(data.get("amount")) or Decimal(0),
        currency=str(data["currency"]),
        balance=money(data.get("balance")),
        description=_str(data.get("description")),
        fee=money(data.get("fee")),
        posted_at=_str(data.get("posted_at")),
        source_type=_str(data.get("source_type")),
        transaction_type=_str(data.get("transaction_type")),
        account_type=_str(data.get("account_type")),
        reference=_str(data.get("reference")),
        status=_str(data.get("status")),
    )


def parse_transfer(data: dict) -> Transfer:
    return Transfer(
        id=str(data["id"]),
        status=str(data["status"]),
        request_id=_str(data.get("request_id")),
        transfer_amount=money(data.get("transfer_amount")),
        transfer_currency=_str(data.get("transfer_currency")),
        source_amount=money(data.get("source_amount")),
        source_currency=_str(data.get("source_currency")),
        fee_amount=money(data.get("fee_amount")),
        fee_currency=_str(data.get("fee_currency")),
        short_reference_id=_str(data.get("short_reference_id")),
        reference=_str(data.get("reference")),
        beneficiary_id=_str(data.get("beneficiary_id")),
        transfer_date=_str(data.get("transfer_date")),
        dispatch_date=_str(data.get("dispatch_date")),
        transfer_method=_str(data.get("transfer_method")),
        failure_reason=_str(data.get("failure_reason")),
        failure_type=_str(data.get("failure_type")),
    )


def parse_quote(data: dict) -> FxQuote:
    quote_id = data.get("quote_id") or data.get("id")
    return FxQuote(
        quote_id=str(quote_id),
        client_rate=money(data.get("client_rate")),
        buy_currency=_str(data.get("buy_currency")),
        sell_currency=_str(data.get("sell_currency")),
        buy_amount=money(data.get("buy_amount")),
        sell_amount=money(data.get("sell_amount")),
        valid_to_at=_str(data.get("valid_to_at") or data.get("valid_to")),
        usage=_str(data.get("usage")),
    )


def _str(value) -> str | None:
    if value is None:
        return None
    return str(value)

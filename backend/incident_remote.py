"""Stateful typed mock of the Airwallex account for the P1-7 incident runner. Offline only.

Money is Decimal. A created transfer debits principal plus its own fee. A return credits the
principal only, so the original fee stays spent. create_transfer is idempotent by request_id and
counts any duplicate POST. Thread safe so concurrent workers can share one account.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from decimal import Decimal

from backend.airwallex.types import (
    Balance,
    BalanceHistoryItem,
    BalanceHistoryPage,
    Beneficiary,
    BeneficiaryValidation,
    CancelResult,
    ConfirmationLetter,
    Conversion,
    FxQuote,
    Transfer,
    TransferPage,
)

ZERO = Decimal(0)


class StatefulRemote:
    def __init__(self, *, eur: str = "100000.00", usd: str = "100000.00", fee: str = "13.47") -> None:
        self._lock = threading.RLock()
        self.balances = {"EUR": Decimal(eur), "USD": Decimal(usd)}
        self.fee = Decimal(fee)
        self.transfers: dict[str, Transfer] = {}
        self.history: list[BalanceHistoryItem] = []
        self.posts: list[dict] = []  # every create_transfer call, accepted or not
        self.duplicate_posts = 0
        self.cancelled: list[str] = []
        self.beneficiaries: list[dict] = []
        self.accept_then_timeout = False
        self.hide_request_lookup = False
        self._serial = 0
        self._quote_serial = 0
        self.rate = Decimal("1.10")
        self.quote_valid_to = "2026-11-03T12:15:00+00:00"
        self.conversion_status = "SETTLED"
        self.quotes: dict[str, FxQuote] = {}
        self.conversions: dict[str, Conversion] = {}
        self.conversion_posts: list[dict] = []
        self.used_quotes: set[str] = set()
        self.log: list[tuple[str, str]] = []  # ordered remote money events
        self.letters: list[tuple[str, str]] = []

    # ---- scenario controls (not part of the adapter surface) ----
    def add_transfer(self, transfer_id: str, *, status: str, amount: str, reference: str,
                     beneficiary_id: str | None, request_id: str, currency: str = "EUR",
                     fee: str | None = None, debit: bool = True) -> Transfer:
        with self._lock:
            fee_amount = self.fee if fee is None else Decimal(fee)
            item = Transfer(
                id=transfer_id, status=status, request_id=request_id,
                transfer_amount=Decimal(amount), transfer_currency=currency,
                fee_amount=fee_amount, fee_currency=currency, reference=reference,
                beneficiary_id=beneficiary_id, transfer_method="SWIFT",
            )
            self.transfers[transfer_id] = item
            if debit:
                self.balances[currency] -= item.transfer_amount + fee_amount
            return item

    def set_dates(self, transfer_id: str, *, transfer_date: str, dispatch_date: str | None = None) -> None:
        with self._lock:
            old = self.transfers[transfer_id]
            self.transfers[transfer_id] = Transfer(**{**old.__dict__, "transfer_date": transfer_date,
                                                      "dispatch_date": dispatch_date})

    def create_confirmation_letter(self, *, transaction_id: str, letter_format: str) -> ConfirmationLetter:
        """Typed stand-in for the Airwallex letter: the live status is printed in the body."""
        with self._lock:
            status = self.transfers[transaction_id].status
            self.letters.append((transaction_id, letter_format))
        return ConfirmationLetter(f"{letter_format} letter {transaction_id} status={status}".encode(), "application/pdf")

    def set_status(self, transfer_id: str, status: str, *, failure_reason: str | None = None) -> None:
        with self._lock:
            old = self.transfers[transfer_id]
            self.transfers[transfer_id] = Transfer(**{**old.__dict__, "status": status,
                                                      "failure_reason": failure_reason or old.failure_reason})

    def bounce(self, transfer_id: str, failure_reason: str) -> None:
        """The bank returns a payment: CANCELLED, principal credited back, fee not refunded."""
        with self._lock:
            old = self.transfers[transfer_id]
            self.set_status(transfer_id, "CANCELLED", failure_reason=failure_reason)
            self.balances[old.transfer_currency or "EUR"] += old.transfer_amount or ZERO
            self.history.append(BalanceHistoryItem(
                id=f"rev-{transfer_id}", source=transfer_id, amount=old.transfer_amount or ZERO,
                currency=old.transfer_currency or "EUR", balance=None, description=None, fee=ZERO,
                posted_at=None, source_type="PAYOUT", transaction_type="PAYOUT_REVERSAL",
                account_type="CASH", reference=old.reference,
            ))

    def eur(self) -> Decimal:
        return self.balances["EUR"]

    # ---- adapter surface used by production code ----
    def get_transfer(self, transfer_id: str) -> Transfer:
        with self._lock:
            return self.transfers[transfer_id]

    def list_transfers(self, **kwargs) -> TransferPage:
        with self._lock:
            wanted = kwargs.get("request_id")
            if wanted is None:
                return TransferPage(items=tuple(self.transfers.values()), page_after=None)
            if self.hide_request_lookup:
                return TransferPage(items=(), page_after=None)
            return TransferPage(
                items=tuple(t for t in self.transfers.values() if t.request_id == wanted), page_after=None
            )

    def balance_history(self, **_kwargs) -> BalanceHistoryPage:
        with self._lock:
            return BalanceHistoryPage(items=tuple(self.history), has_more=False, page_after=None)

    def current_balances(self) -> list[Balance]:
        with self._lock:
            return [
                Balance(cur, value, ZERO, ZERO, value, ZERO, account_type="cash")
                for cur, value in self.balances.items()
            ]

    def create_transfer(self, **kwargs) -> Transfer:
        with self._lock:
            self.posts.append(kwargs)
            request_id = kwargs["request_id"]
            if any(t.request_id == request_id for t in self.transfers.values()):
                self.duplicate_posts += 1
                raise RuntimeError("duplicate_request_id")
            self._serial += 1
            new = self.add_transfer(
                f"awx-new-{self._serial}", status="SCHEDULED", amount=str(kwargs["transfer_amount"]),
                currency=kwargs["transfer_currency"], reference=kwargs["reference"],
                beneficiary_id=kwargs["beneficiary_id"], request_id=request_id,
            )
            self.log.append(("transfer", new.id))
            if self.accept_then_timeout:
                self.accept_then_timeout = False
                raise TimeoutError("bank accepted; response lost")
            return new

    def cancel_transfer(self, transfer_id: str, **_kwargs) -> CancelResult:
        with self._lock:
            old = self.transfers[transfer_id]
            if old.status != "SCHEDULED":
                return CancelResult(ok=False, transfer=old)
            self.set_status(transfer_id, "CANCELLED")
            self.balances[old.transfer_currency or "EUR"] += (old.transfer_amount or ZERO) + (old.fee_amount or ZERO)
            self.cancelled.append(transfer_id)
            return CancelResult(ok=True, transfer=self.transfers[transfer_id])

    # ---- FX: quotes and conversions ----
    def create_quote(self, **kwargs) -> FxQuote:
        with self._lock:
            self._quote_serial += 1
            buy = kwargs["buy_amount"]
            quote = FxQuote(
                quote_id=f"quo_{self._quote_serial}", client_rate=self.rate, buy_currency=kwargs["buy_currency"],
                sell_currency=kwargs["sell_currency"], buy_amount=buy,
                sell_amount=(buy * self.rate).quantize(Decimal("0.01")), valid_to_at=self.quote_valid_to,
                usage="SINGLE_USE",
            )
            self.quotes[quote.quote_id] = quote
            return quote

    def create_conversion(self, **kwargs) -> Conversion:
        with self._lock:
            request_id = kwargs["request_id"]
            self.conversion_posts.append(kwargs)
            if request_id in self.conversions:
                self.duplicate_posts += 1
                raise RuntimeError("duplicate_request_id")
            quote = self.quotes[kwargs["quote_id"]]
            if quote.quote_id in self.used_quotes:
                raise RuntimeError("quote already used")
            self.used_quotes.add(quote.quote_id)
            conversion = Conversion(conversion_id=f"con_{len(self.conversions) + 1}",
                                    status=self.conversion_status, quote_id=quote.quote_id)
            self.conversions[request_id] = conversion
            self.log.append(("conversion", conversion.conversion_id))
            if conversion.status == "SETTLED":
                self._apply(quote)
            return conversion

    def settle_conversion(self, conversion_id: str) -> None:
        with self._lock:
            for request_id, conversion in self.conversions.items():
                if conversion.conversion_id == conversion_id and conversion.status != "SETTLED":
                    self.conversions[request_id] = replace(conversion, status="SETTLED")
                    self._apply(self.quotes[conversion.quote_id])

    def find_conversion(self, request_id: str):
        with self._lock:
            return self.conversions.get(request_id)

    def _apply(self, quote: FxQuote) -> None:
        self.balances[quote.buy_currency] += quote.buy_amount
        self.balances[quote.sell_currency] -= quote.sell_amount

    def validate_beneficiary(self, _body) -> BeneficiaryValidation:
        return BeneficiaryValidation(ok=True)

    def create_beneficiary(self, body) -> Beneficiary:
        with self._lock:
            self.beneficiaries.append(body)
            return Beneficiary(id=f"ben_new_{len(self.beneficiaries)}")

    def find_beneficiary(self, request_id: str):
        with self._lock:
            for index, body in enumerate(self.beneficiaries, start=1):
                if body.get("request_id") == request_id:
                    return Beneficiary(id=f"ben_new_{index}")
            return None

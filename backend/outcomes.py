"""One reducer for original and colleague outcomes. Unknown evidence never counts as success."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

LIVE_STATUSES = frozenset({"SCHEDULED", "PROCESSING", "SENT", "PAID", "OVERDUE"})
NOT_PAID_STATUSES = frozenset({"FAILED", "CANCELLED"})


def reduce_outcome(*, original_returned: bool, colleague_status: str | None) -> str:
    """close: colleague PAID. track: still moving. reopen: FAILED/CANCELLED. hold: unknown.

    The original must be independently returned before any outcome other than hold."""
    if not original_returned:
        return "hold"
    if colleague_status == "PAID":
        return "close"
    if colleague_status in LIVE_STATUSES:
        return "track"
    if colleague_status in NOT_PAID_STATUSES:
        return "reopen"
    return "hold"


class HistoryError(Exception):
    """Balance history cannot be read completely: repeated or missing cursor, or too many pages."""


_MAX_PAGES = 5000
_POSTED = frozenset({"POSTED", "SETTLED", "COMPLETED", "SUCCESS", "SUCCEEDED"})


def _iter_reversals(client, transfer_id: str, currency: str | None):
    """Every PAYOUT_REVERSAL for the transfer, across all pages. Bad pagination raises HistoryError."""
    if not currency:
        return
    page_token = None
    seen_cursors: set[str] = set()
    for _ in range(_MAX_PAGES):
        history = client.balance_history(currency=currency, page_size=10, page=page_token)
        for item in history.items:
            if item.transaction_type == "PAYOUT_REVERSAL" and item.source == transfer_id:
                yield item
        if not history.has_more:
            return
        if not history.page_after:
            raise HistoryError("missing cursor")
        if history.page_after in seen_cursors:
            raise HistoryError("repeated cursor")
        seen_cursors.add(history.page_after)
        page_token = history.page_after
    raise HistoryError("too many pages")


def reversal_for(client, transfer_id: str, currency: str | None):
    """First PAYOUT_REVERSAL for a transfer, or None. Any reversal, even partial, means the payment failed."""
    return next(_iter_reversals(client, transfer_id, currency), None)


def reversals_for(client, transfer_id: str, currency: str | None) -> list:
    return list(_iter_reversals(client, transfer_id, currency))


def refund_principal(remote) -> tuple[Decimal, str] | None:
    """The amount and currency the original debited. None when it cannot be known (hold)."""
    amount, currency = remote.transfer_amount, remote.transfer_currency
    source_amount, source_currency = remote.source_amount, remote.source_currency
    if source_amount is None and source_currency is not None and source_currency != currency:
        return None  # cross-currency debit without its amount
    if source_amount is not None:
        if source_currency is None:
            return None
        amount, currency = source_amount, source_currency
    value = _positive(amount)
    if value is None or not isinstance(currency, str) or not currency:
        return None
    return value, currency


def _positive(raw) -> Decimal | None:
    """A positive finite Decimal, or None for anything missing, malformed, zero, negative or non-finite."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(raw) if isinstance(raw, (Decimal, int)) else Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() and value > 0 else None


def refund_currency(remote, fallback: str | None = None) -> str | None:
    principal = refund_principal(remote)
    return principal[1] if principal else (remote.transfer_currency or fallback)


def full_refund(remote, reversals: list):
    """The single posted credit equal to the original principal, or None. Ambiguity is None."""
    principal = refund_principal(remote)
    if principal is None or len(reversals) != 1:
        return None
    item = reversals[0]
    if item.transaction_type != "PAYOUT_REVERSAL" or not item.source or item.source != remote.id:
        return None
    # Only a CASH credit is spendable cash. CREDIT, YIELD, unknown, empty or missing never count.
    if str(getattr(item, "account_type", None) or "").strip().lower() != "cash":
        return None
    amount = _positive(item.amount)
    if amount is None or (amount, item.currency) != principal:
        return None
    # Balance history is the posted ledger: a row with no status is posted. An explicit status must be posted.
    if item.status is not None and str(item.status).upper() not in _POSTED:
        return None
    if item.posted_at == "":
        return None
    return item


def original_returned(client, original) -> bool:
    """True only when the original is CANCELLED remotely and its full principal is credited back."""
    if original is None or not original["airwallex_id"]:
        return False
    remote = client.get_transfer(original["airwallex_id"])
    if remote.status != "CANCELLED":
        return False
    try:
        items = reversals_for(client, original["airwallex_id"], refund_currency(remote))
    except HistoryError:
        return False
    return full_refund(remote, items) is not None


@dataclass(frozen=True)
class Outcome:
    """One payment for the obligation. `credited` is False for an unowned supplier."""

    airwallex_id: str
    role: str  # "original" or "replacement"
    status: str | None
    reversed: bool = False
    returned: bool = False  # original only: CANCELLED with a non-zero reversal
    credited: bool = True
    fresh: bool = True  # False when the read failed and only stored facts are known


def outcome_kind(outcome: Outcome) -> str:
    """paid | live | dead | unknown. A reversal or a terminal failure is dead, never paid."""
    if outcome.reversed or outcome.status in NOT_PAID_STATUSES:
        return "dead"
    if outcome.status == "PAID":
        return "paid" if outcome.credited else "live"
    if outcome.status in LIVE_STATUSES:
        return "live"
    return "unknown"


def decide_set(
    outcomes: list[Outcome], *, incomplete: bool, was_closed: bool, pending: bool = False
) -> str:
    """One decision from the COMPLETE set, independent of row order.

    close: exactly one credited PAID and every other outcome terminal and accounted for.
    reopen: nothing is paid. A later failure or reversal wins over an earlier closure.
    conflict: two PAID. hold: anything pending, unknown, ambiguous or unread."""
    kinds = [(item, outcome_kind(item)) for item in outcomes]
    paid = [item for item, kind in kinds if kind == "paid"]
    dead = any(kind == "dead" for _item, kind in kinds)
    unsettled = incomplete or any(kind in ("live", "unknown") for _item, kind in kinds)
    original = next((item for item in outcomes if item.role == "original"), None)
    # New confirmed live/unknown evidence, or a recorded attempt still unresolved, voids a closure.
    # A pure read failure of facts already accounted for only holds the prior closure.
    material = pending or any(item.fresh and kind in ("live", "unknown") for item, kind in kinds)
    if len(paid) >= 2:
        return "conflict"
    if was_closed and (material or (dead and not paid)):
        return "reopen"
    if not paid:
        return "hold" if unsettled or original is None else "reopen"
    if unsettled or original is None:
        return "hold"
    if paid[0].role != "original" and not original.returned:
        return "hold"
    return "close"

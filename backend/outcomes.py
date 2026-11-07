"""One reducer for original and colleague outcomes. Unknown evidence never counts as success."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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


def reversal_for(client, transfer_id: str, currency: str | None):
    """Find the PAYOUT_REVERSAL for a transfer in recent balance history, or None."""
    if not currency:
        return None
    page_token = None
    for _ in range(5):
        history = client.balance_history(currency=currency, page_size=10, page=page_token)
        for item in history.items:
            if item.transaction_type == "PAYOUT_REVERSAL" and item.source == transfer_id:
                return item
        if not history.has_more or not history.page_after:
            return None
        page_token = history.page_after
    return None


def original_returned(client, original) -> bool:
    """True only when the original is CANCELLED remotely and its reversal has an amount."""
    if original is None or not original["airwallex_id"]:
        return False
    remote = client.get_transfer(original["airwallex_id"])
    if remote.status != "CANCELLED":
        return False
    reversal = reversal_for(client, original["airwallex_id"], remote.transfer_currency)
    if reversal is None or reversal.amount is None:
        return False
    return Decimal(reversal.amount) != 0


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

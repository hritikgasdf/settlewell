"""Replace stays locked until the original principal is back. No network."""

from decimal import Decimal

import pytest

from backend.airwallex.types import BalanceHistoryItem, BalanceHistoryPage, Transfer
from backend.ledger import connect, seed_hero
from backend.replace_payment import ReplaceError
from backend.replacement_tool import propose_replacement, refund_card


class Wallet:
    def __init__(self, *, reversal: bool) -> None:
        self.reversal = reversal
        self.created = 0

    def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer(
            id=transfer_id,
            status="CANCELLED",
            transfer_amount=Decimal(4200),
            transfer_currency="EUR",
            transfer_method="SWIFT",
            fee_amount=Decimal("13.47"),
            reference="INV-2291",
        )

    def balance_history(self, **_kwargs) -> BalanceHistoryPage:
        if not self.reversal:
            return BalanceHistoryPage(items=(), has_more=False, page_after=None)
        return BalanceHistoryPage(
            items=(
                BalanceHistoryItem(
                    id="rev-1",
                    source="awx-other",
                    amount=Decimal(4200),
                    currency="EUR",
                    balance=None,
                    description=None,
                    fee=Decimal(0),
                    posted_at=None,
                    source_type="PAYOUT",
                    transaction_type="PAYOUT_REVERSAL",
                    account_type=None,
                    reference=None,
                ),
                BalanceHistoryItem(
                    id="rev-2",
                    source="awx-hero",
                    amount=Decimal(4200),
                    currency="EUR",
                    balance=None,
                    description="Refund",
                    fee=Decimal(0),
                    posted_at=None,
                    source_type="PAYOUT",
                    transaction_type="PAYOUT_REVERSAL",
                    account_type=None,
                    reference="INV-2291",
                ),
            ),
            has_more=False,
            page_after=None,
        )

    def create_transfer(self, **_kwargs):
        self.created += 1
        raise AssertionError("create should wait for the refund card")


def _ids(conn):
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    return obligation_id, str(incident_id)


def test_replace_stays_locked_until_the_refund_card_shows_the_fee():
    conn = connect()
    obligation_id, incident_ref = _ids(conn)
    locked = refund_card(conn, Wallet(reversal=False), obligation_id=obligation_id)
    assert locked.unlocked is False
    assert locked.text is None
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(
            conn,
            Wallet(reversal=False),
            obligation_id=obligation_id,
            incident_ref=incident_ref,
        )
    assert caught.value.code == "refund"

    shown = refund_card(conn, Wallet(reversal=True), obligation_id=obligation_id)
    assert shown.unlocked is True
    assert shown.text == "principal back: €4,200 · fee lost €13.47"

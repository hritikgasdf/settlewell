"""After a return, a colleague's payment closes the incident. No second replacement. No network."""

from decimal import Decimal

import pytest

from backend.airwallex.types import BalanceHistoryItem, BalanceHistoryPage, Transfer, TransferPage
from backend.ledger import connect, seed_hero
from backend.repaid import colleague_repaid
from backend.replace_payment import ReplaceError
from backend.replacement_tool import propose_replacement


def _transfer(transfer_id: str, status: str, request_id: str, **overrides) -> Transfer:
    fields = {
        "id": transfer_id,
        "status": status,
        "request_id": request_id,
        "transfer_amount": Decimal("4200.00"),
        "transfer_currency": "EUR",
        "reference": "INV-2291",
        "beneficiary_id": "ben_colleague",
    }
    fields.update(overrides)
    return Transfer(**fields)


class Account:
    def __init__(self, items: tuple[Transfer, ...]) -> None:
        self.items = items
        self.created: list[dict] = []

    def list_transfers(self, **_kwargs):
        return TransferPage(items=self.items, page_after=None)

    def get_transfer(self, transfer_id: str) -> Transfer:
        for item in self.items:
            if item.id == transfer_id:
                return item
        return _transfer(transfer_id, "CANCELLED", "original-payment", beneficiary_id="ben_old")

    def balance_history(self, **_kwargs) -> BalanceHistoryPage:
        return BalanceHistoryPage(
            items=(
                BalanceHistoryItem(
                    id="rev-1",
                    source="awx-hero",
                    amount=Decimal("4200.00"),
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

    def create_transfer(self, **kwargs):
        self.created.append(kwargs)
        raise AssertionError("a colleague payment must not be replaced")


def _returned(conn):
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    conn.execute(
        "UPDATE transfer_record SET status = 'CANCELLED', request_id = 'original-payment' WHERE airwallex_id = 'awx-hero'"
    )
    supplier_id = conn.execute(
        "SELECT supplier_id FROM obligation WHERE id = ?",
        (obligation_id,),
    ).fetchone()["supplier_id"]
    conn.execute(
        """
        INSERT INTO bank_detail_version (
            supplier_id, beneficiary_id, details_hash, bank_name, account_last4, source,
            verified_by, verified_at
        ) VALUES (?, 'ben_verified', 'callback-v2', 'Deutsche Kreditbank', '7781',
                  'verified_callback', 'finance', '2026-11-03T12:00:00+00:00')
        """,
        (supplier_id,),
    )
    conn.commit()
    return obligation_id, str(incident_id)


def test_a_colleague_payment_closes_the_incident_and_blocks_replacement():
    conn = connect()
    obligation_id, incident_ref = _returned(conn)
    colleague = _transfer("awx-colleague", "PAID", "colleague-hand")
    account = Account(
        (
            _transfer("awx-hero", "CANCELLED", "original-payment", beneficiary_id="ben_old"),
            colleague,
            _transfer("awx-other", "PAID", "other-invoice", reference="INV-9999"),
        )
    )

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    again = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "close"
    assert decision.airwallex_id == "awx-colleague"
    assert again.action == "close"
    incident = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()
    assert incident["state"] == "CLOSED"
    live = conn.execute(
        """
        SELECT transfer_record.airwallex_id, transfer_record.origin
        FROM obligation JOIN transfer_record ON transfer_record.id = obligation.live_transfer_id
        WHERE obligation.id = ?
        """,
        (obligation_id,),
    ).fetchone()
    assert live["airwallex_id"] == "awx-colleague"
    assert live["origin"] == "external"
    obligation = conn.execute("SELECT status FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    assert obligation["status"] == "PAID_ONCE"
    audits = conn.execute(
        "SELECT kind, detail FROM audit_event WHERE kind = 'colleague_repaid'"
    ).fetchall()
    assert len(audits) == 1
    assert audits[0]["detail"] == "awx-colleague"

    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code == "repaid"
    assert account.created == []


def test_a_scheduled_extra_offers_cancel_when_another_live_payment_exists():
    conn = connect()
    obligation_id, incident_ref = _returned(conn)
    account = Account(
        (
            _transfer("awx-hero", "CANCELLED", "original-payment"),
            _transfer("awx-paid", "PAID", "colleague-hand", beneficiary_id="ben_a"),
            _transfer("awx-friday", "SCHEDULED", "colleague-friday", beneficiary_id="ben_b"),
        )
    )

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "cancel"
    assert decision.airwallex_id == "awx-friday"
    incident = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()
    assert incident["state"] == "OPEN"
    stored = conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE origin = 'external' ORDER BY airwallex_id"
    ).fetchall()
    assert [row["airwallex_id"] for row in stored] == ["awx-friday", "awx-paid"]
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code == "repaid"
    assert account.created == []


def test_a_return_without_a_colleague_payment_still_allows_a_replacement_decision():
    conn = connect()
    obligation_id, incident_ref = _returned(conn)
    account = Account((_transfer("awx-hero", "CANCELLED", "original-payment"),))

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "replace"
    assert decision.airwallex_id is None
    incident = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()
    assert incident["state"] == "OPEN"

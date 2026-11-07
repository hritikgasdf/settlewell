"""Incident timeline from polling. No network."""

from decimal import Decimal

from backend.airwallex.types import BalanceHistoryItem, BalanceHistoryPage, Transfer, TransferPage
from backend.ledger import connect, remember_failure_reason, seed_hero
from backend.reconcile import poll_incident


def _transfer(transfer_id: str, status: str, **overrides) -> Transfer:
    fields = {
        "id": transfer_id,
        "status": status,
        "request_id": "stl-inv2291-1",
        "transfer_amount": Decimal("4200.00"),
        "transfer_currency": "EUR",
        "reference": "INV-2291",
        "failure_reason": None,
    }
    fields.update(overrides)
    return Transfer(**fields)


def _reversal() -> BalanceHistoryItem:
    return BalanceHistoryItem(
        id="rev-1",
        source="awx-hero",
        amount=Decimal("4200.00"),
        currency="EUR",
        balance=None,
        description="Refund",
        fee=Decimal(0),
        posted_at="2026-11-03T12:00:00+00:00",
        source_type="PAYOUT",
        transaction_type="PAYOUT_REVERSAL",
        account_type=None,
        reference="INV-2291",
    )


class ClosedApi:
    def get_transfer(self, transfer_id):
        if transfer_id == "awx-dup":
            return _transfer("awx-dup", "SCHEDULED", request_id="colleague-friday")
        return _transfer(
            transfer_id,
            "CANCELLED",
            failure_reason="90701: Account closed",
            failure_type="INCORRECT_ROUTING",
        )

    def balance_history(self, **_kwargs):
        return BalanceHistoryPage(items=(_reversal(),), has_more=False, page_after=None)

    def list_transfers(self, **_kwargs):
        return TransferPage(
            items=(
                self.get_transfer("awx-hero"),
                _transfer("awx-dup", "SCHEDULED", request_id="colleague-friday"),
            ),
            page_after=None,
        )


def test_poll_builds_a_timeline_and_tags_the_external_lookalike():
    conn = connect()
    incident_id = seed_hero(conn, airwallex_id="awx-hero")

    first = poll_incident(conn, ClosedApi(), incident_id, seen_at="2026-11-03T12:00:01+00:00")
    second = poll_incident(conn, ClosedApi(), incident_id, seen_at="2026-11-03T12:00:02+00:00")

    kinds = {entry["kind"] for entry in first}
    assert {"status", "payout_reversal", "external_lookalike"} <= kinds
    assert any(entry["detail"] == "CANCELLED" for entry in first)
    assert any("PAYOUT_REVERSAL source=awx-hero" in entry["detail"] for entry in first)
    stored = conn.execute(
        "SELECT status, failure_code FROM transfer_record WHERE airwallex_id = 'awx-hero'"
    ).fetchone()
    assert stored["status"] == "CANCELLED"
    assert stored["failure_code"] == "90701"
    external = conn.execute(
        "SELECT origin, request_id FROM transfer_record WHERE airwallex_id = 'awx-dup'"
    ).fetchone()
    assert external["origin"] == "external"
    assert external["request_id"] == "colleague-friday"
    assert len(second) == len(first)
    # one snapshot for the original and one for the tagged external; the refresh adds none
    assert conn.execute("SELECT COUNT(*) AS n FROM status_snapshot").fetchone()["n"] == 2
    assert conn.execute("SELECT status FROM obligation").fetchone()["status"] == "OPEN"


def test_a_null_poll_does_not_clear_a_stored_failure_reason():
    conn = connect()
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    transfer_id = conn.execute(
        "SELECT id FROM transfer_record WHERE airwallex_id = 'awx-hero'"
    ).fetchone()["id"]
    remember_failure_reason(conn, transfer_id, "90701: Account closed")
    conn.commit()

    class NullReason:
        def get_transfer(self, transfer_id):
            return _transfer(transfer_id, "CANCELLED", failure_reason=None)

        def balance_history(self, **_kwargs):
            return BalanceHistoryPage(items=(), has_more=False, page_after=None)

        def list_transfers(self, **_kwargs):
            return TransferPage(items=(), page_after=None)

    poll_incident(conn, NullReason(), incident_id, seen_at="2026-11-03T12:00:03+00:00")
    row = conn.execute(
        "SELECT failure_message FROM transfer_record WHERE airwallex_id = 'awx-hero'"
    ).fetchone()
    assert row["failure_message"] == "90701: Account closed"


def test_paid_from_a_get_is_provisional_until_a_later_reversal():
    conn = connect()
    incident_id = seed_hero(conn, airwallex_id="awx-hero")

    class Paid:
        def get_transfer(self, transfer_id):
            return _transfer(transfer_id, "PAID")

        def balance_history(self, **_kwargs):
            return BalanceHistoryPage(items=(), has_more=False, page_after=None)

        def list_transfers(self, **_kwargs):
            return TransferPage(items=(), page_after=None)

    poll_incident(conn, Paid(), incident_id, seen_at="2026-11-03T12:00:04+00:00")
    assert conn.execute("SELECT status FROM obligation").fetchone()["status"] == "PAID_ONCE"

    poll_incident(conn, ClosedApi(), incident_id, seen_at="2026-11-03T12:00:05+00:00")
    assert conn.execute("SELECT status FROM obligation").fetchone()["status"] == "IN_FLIGHT"

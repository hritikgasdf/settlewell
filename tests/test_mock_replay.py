"""The hero replay is deterministic and offline. No network and no secrets."""

import inspect
from decimal import Decimal

import pytest

from backend.airwallex.client import AirwallexClient
from backend.console import ConsoleError, render
from backend.ledger import connect
from backend.mock_replay import play_hero, render_replay


def test_two_replays_are_identical():
    assert play_hero() == play_hero()
    assert render_replay() == render_replay()


def test_the_replay_walks_the_hero_without_a_client():
    replay = play_hero()

    assert replay.duplicate_fee == "0.00"
    assert replay.failure_rule == "R-REPLACE_NEW_DETAILS-90701"
    assert replay.proof_while_sent is True
    assert replay.proof_after_cancel is False
    assert "cancelled" in replay.proof_refusal
    assert replay.bank_card == "Commerzbank → DKB ···4400 → ···7781"
    assert replay.refund_text == "principal back: €4,200 · fee lost €13.47"
    assert replay.quote_id == "quo_hero"
    assert replay.quote_state == "CONSUMED"
    assert replay.reuse_code == "consumed"
    assert replay.proposer == "ap-clerk"
    assert replay.approver == "finance-approver"
    assert replay.proposer != replay.approver
    assert replay.created_count == 1
    assert replay.second_replacement == "repaid"
    assert replay.paid_once is True
    assert replay.incident_cost == "13.47"
    assert replay.eur_before == "800.00"
    # Old 5200.00/95160.00 were the post-FX cash for a 4400 top-up that ignored the payout fee
    # and never debited the payout. The gate now budgets principal + 26.94 conservative fee
    # (2 x 13.47) + 1000 floor, so the top-up is 4426.94 (sold at 1.10 = 4869.63 USD). The fake bank
    # debits only the ACTUAL 13.47 fee, so 13.47 of the budget stays as unused buffer above the floor.
    assert replay.eur_after_fx == "5226.94"
    assert replay.eur_after == "1013.47"  # 5226.94 - 4200.00 - 13.47 actual fee
    assert Decimal(replay.eur_after) >= Decimal("1000.00")
    assert Decimal(replay.eur_after_fx) - Decimal(replay.eur_after) == Decimal("4213.47")
    assert replay.usd_before == "100000.00"
    assert replay.usd_after == "95130.37"  # 100000.00 - 4426.94 * 1.10 = 4869.634, rounded


def test_the_page_is_labelled_mock_and_escapes_output(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    page = render("/replay")
    assert 'class="banner">MOCK' in page
    assert "Cancelled at €0" in page
    assert "R-REPLACE_NEW_DETAILS-90701" in page
    assert "principal back: €4,200 · fee lost €13.47" in page
    assert "quo_hero" in page
    assert "consumed" in page
    assert "INV-2291 paid once" in page
    assert "Incident cost €13.47" in page
    assert 'id="fidelity"' in page
    assert "not Airwallex API fields" in page
    assert "does not call Airwallex" in page
    assert "<script>" not in page
    assert "AirwallexClient" not in page


def test_the_replay_page_refuses_to_render_outside_mock(monkeypatch):
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    with pytest.raises(ConsoleError) as caught:
        render("/replay")
    assert caught.value.code == "mock"


def test_the_module_does_not_construct_a_live_client():
    import backend.mock_replay as replay

    source = inspect.getsource(replay)
    assert "AirwallexClient" not in source
    assert "Authorization" not in source
    assert "api-demo.airwallex.com" not in source
    assert not isinstance(replay.HeroBook(), AirwallexClient)


def test_a_passed_ledger_keeps_one_replacement_and_a_zero_fee_duplicate():
    conn = connect()
    replay = play_hero(conn)
    live = conn.execute(
        """
        SELECT airwallex_id FROM transfer_record
        WHERE origin = 'settlewell'
          AND status IN ('SCHEDULED', 'PROCESSING', 'SENT', 'PAID', 'OVERDUE')
        """
    ).fetchall()
    duplicate = conn.execute(
        "SELECT status, fee FROM transfer_record WHERE airwallex_id = 'awx-dup'"
    ).fetchone()
    assert replay.replacement_id == "awx-replace"
    assert [row["airwallex_id"] for row in live] == ["awx-replace"]
    assert duplicate["status"] == "CANCELLED"
    assert duplicate["fee"] == "0.00"

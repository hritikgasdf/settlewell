"""A replacement paid from an FX top-up waits for a SETTLED conversion. Reached through execute(). No network."""

from decimal import Decimal

import pytest
from test_execute import EXPIRES, SEEN, _grant, _ready
from test_funding_execute import Funded, _setup, rows

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import Balance, Conversion, FxQuote
from backend.approvals import AP_CLERK, FINANCE_APPROVER, approve
from backend.execute import execute, intent_terms, open_intent
from backend.fx_funding import FundingError, fund_replacement, reserve_floor
from backend.ledger import connect
from backend.replace_payment import ReplaceError

NEED = Decimal("4200.00") + Decimal("13.47") + Decimal("1000.00")


class Fx(Funded):
    """EUR available is set by the test, never predicted. Lookup answers are scripted."""

    def __init__(self, eur="500.00", *, original="4200.00"):
        super().__init__()
        self.originals["awx-hero"] = original
        self.eur = Decimal(eur)
        self.status = "PENDING"
        self.lookup = "found"  # "found" | "none" | "error"
        self.lookups: list[str] = []
        self.quote_calls: list[dict] = []

    def current_balances(self):
        return rows(str(self.eur))

    def create_quote(self, **kwargs):
        self.quote_calls.append(kwargs)
        return FxQuote(
            quote_id="quo_settle", client_rate=Decimal("1.10"), buy_currency="EUR", sell_currency="USD",
            buy_amount=kwargs["buy_amount"], sell_amount=kwargs["buy_amount"],
            valid_to_at="2026-11-03T12:15:00+00:00", usage="MULTI_USE",
        )

    def create_conversion(self, **kwargs):
        self.conversions.append(kwargs)
        return Conversion(conversion_id="con_settle", status=self.status, quote_id=kwargs["quote_id"])

    def find_conversion(self, request_id):
        self.lookups.append(request_id)
        if self.lookup == "error":
            raise AirwallexError(503, "down", "")
        if self.lookup == "none":
            return None
        return Conversion(conversion_id="con_settle", status=self.status, quote_id="quo_settle")


def _flow(client):
    """Plan, approve and run the conversion once (status as scripted), and grant the payout."""
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    locked = fund_replacement(conn, client, obligation_id=obligation_id, as_of=SEEN)
    assert locked.action == "locked"
    fx = open_intent(
        conn, kind="fx_conversion", incident_id=incident_id, obligation_id=obligation_id, action="FX_FUND",
        amount=format(locked.buy_amount, "f"), currency="EUR", beneficiary_id="ben_verified",
        details_hash="callback-v2", proposer=AP_CLERK, quote_id=locked.quote_id, expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(conn, fx.approval_id, intent_terms(conn, fx.id), approver=FINANCE_APPROVER, used_at=SEEN)
    execute(conn, client, fx.id, as_of=SEEN)
    return conn, _grant(conn, incident_id, quote_id=locked.quote_id), fx


def _refused(conn, client, ref, code):
    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)
    assert caught.value.code == code
    assert client.created == []


def test_the_real_conversion_status_is_stored_not_converted():
    client = Fx()
    conn, _ref, fx = _flow(client)
    row = conn.execute("SELECT remote_id, remote_status FROM execution_intent WHERE id = ?", (fx.id,)).fetchone()
    assert (row["remote_id"], row["remote_status"]) == ("con_settle", "PENDING")


def test_a_pending_conversion_blocks_the_payout_even_with_inflated_available():
    client = Fx()
    conn, ref, _fx = _flow(client)
    client.eur = Decimal("999999.00")  # available looks big, settlement is not proven
    _refused(conn, client, ref, "conversion")
    assert len(client.conversions) == 1
    assert client.lookups  # refreshed by the stored request id, never created again


def test_lookup_unavailable_or_empty_stays_pending():
    for mode in ("error", "none"):
        client = Fx()
        conn, ref, _fx = _flow(client)
        client.status, client.lookup, client.eur = "SETTLED", mode, Decimal("999999.00")
        _refused(conn, client, ref, "conversion")
        assert len(client.conversions) == 1


def test_settled_but_balance_short_stays_blocked_then_pays_exactly_once():
    client = Fx()
    conn, ref, fx = _flow(client)
    client.status = "SETTLED"
    client.eur = NEED - Decimal("0.01")
    _refused(conn, client, ref, "balance")
    assert len(client.conversions) == 1
    client.eur = NEED  # exactly amount + fee + floor, read live
    result = execute(conn, client, ref.id, as_of=SEEN)
    assert result.remote_id == "awx-new" and len(client.created) == 1
    assert len(client.conversions) == 1
    row = conn.execute("SELECT remote_status FROM execution_intent WHERE id = ?", (fx.id,)).fetchone()
    assert row["remote_status"] == "SETTLED"
    execute(conn, client, ref.id, as_of=SEEN)
    assert len(client.created) == 1 and len(client.conversions) == 1


def test_planning_uses_the_live_original_amount_and_fee_not_the_local_amount():
    client = Fx("800.00", original="4300.00")
    conn = connect()
    obligation_id, _incident = _ready(conn)
    assert conn.execute("SELECT amount FROM obligation").fetchone()["amount"] == "4200.00"
    result = fund_replacement(conn, client, obligation_id=obligation_id, as_of=SEEN)
    expected = Decimal("4300.00") + Decimal("13.47") + Decimal("1000.00") - Decimal("800.00")
    assert result.buy_amount == expected
    assert client.quote_calls[0]["buy_amount"] == expected
    assert client.conversions == [] and client.created == []


def test_planning_fails_closed_on_missing_original_fee_or_balance():
    client = Fx("800.00")
    conn = connect()
    obligation_id, _incident = _ready(conn)
    client.fee = None
    with pytest.raises(FundingError):
        fund_replacement(conn, client, obligation_id=obligation_id, as_of=SEEN)
    client.fee = Decimal("13.47")
    conn.execute("UPDATE obligation SET live_transfer_id = NULL")
    with pytest.raises(FundingError):
        fund_replacement(conn, client, obligation_id=obligation_id, as_of=SEEN)
    assert client.quote_calls == []


@pytest.mark.parametrize("raw", ["NaN", "inf", "-inf", "0", "abc"])
def test_an_invalid_floor_holds_planning(monkeypatch, raw):
    monkeypatch.setenv("SETTLEWELL_RESERVE_FLOOR", raw)
    with pytest.raises(FundingError):
        reserve_floor()
    client = Fx("800.00")
    conn = connect()
    obligation_id, _incident = _ready(conn)
    with pytest.raises(FundingError):
        fund_replacement(conn, client, obligation_id=obligation_id, as_of=SEEN)
    assert client.quote_calls == []


def _bal(currency, amount, kind):
    value = Decimal(amount)
    return Balance(currency, value, Decimal(0), Decimal(0), value, Decimal(0), account_type=kind)


def _paying(*eur_rows):
    return _setup(balances=[[*eur_rows, _bal("USD", "100000.00", "CASH")]])


@pytest.mark.parametrize("other", ["YIELD", "credit"])
def test_a_non_cash_balance_cannot_fund_a_payout(other):
    conn, client, ref = _paying(_bal("EUR", "100.00", "CASH"), _bal("EUR", "999999.00", other))
    _refused(conn, client, ref, "balance")


def test_cash_sufficient_with_a_yield_row_pays():
    conn, client, ref = _paying(_bal("EUR", str(NEED), "cash"), _bal("EUR", "1.00", "YIELD"))
    assert execute(conn, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert len(client.created) == 1


@pytest.mark.parametrize("kinds", [(None,), ("MYSTERY",), ("CASH", "CASH")])
def test_unknown_or_ambiguous_account_type_holds(kinds):
    conn, client, ref = _paying(*(_bal("EUR", "999999.00", kind) for kind in kinds))
    _refused(conn, client, ref, "balance")


def test_planning_reads_only_cash_balances():
    client = Fx("800.00")
    usd = _bal("USD", "100000.00", "CASH")
    client.current_balances = lambda: [_bal("EUR", "800.00", "CASH"), _bal("EUR", "999999.00", "YIELD"), usd]
    conn = connect()
    obligation_id, _incident = _ready(conn)
    assert fund_replacement(conn, client, obligation_id=obligation_id, as_of=SEEN).action == "locked"
    client.current_balances = lambda: [_bal("EUR", "800.00", "CASH"), _bal("EUR", "5.00", None), usd]
    with pytest.raises(FundingError):
        fund_replacement(conn, client, obligation_id=obligation_id, as_of=SEEN)

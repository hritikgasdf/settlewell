"""A quote is used once. An expired quote is replaced and approved again. No network."""

import threading
from decimal import Decimal

import pytest

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import Conversion, FxQuote
from backend.approvals import AP_CLERK, FINANCE_APPROVER, ApprovalError, ApprovalTerms, binding_hash
from backend.approvals import approve as approve_grant
from backend.execute import ExecuteError, execute, intent_terms, open_intent
from backend.fx_quote import FxQuoteError, approve_fx_quote, execute_fx_quote, propose_fx_quote
from backend.ledger import LedgerError, connect, consume_fx_quote, lock_fx_quote, migrate, seed_hero

EARLY = "2026-11-03T12:30:00+00:00"
VALID = "2026-11-03T13:00:00+00:00"
LATE = "2026-11-03T13:00:00+00:00"
USED = "2026-11-03T12:40:00+00:00"


def _terms(quote_id: str = "quo_1") -> ApprovalTerms:
    return ApprovalTerms(
        action="FX_FUND",
        amount="4400.00",
        currency="EUR",
        beneficiary_id="ben_dkb",
        details_hash="hash-dkb",
        quote_id=quote_id,
    )


class Desk:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.created: list[dict] = []
        self.converted: list[dict] = []
        self._next = 1

    def create_quote(self, **kwargs) -> FxQuote:
        self._next += 1
        self.created.append(kwargs)
        buy = kwargs["buy_amount"]
        return FxQuote(
            quote_id=f"quo_{self._next}",
            client_rate=Decimal("1.10"),
            buy_currency="EUR",
            sell_currency="USD",
            buy_amount=buy,
            sell_amount=buy,
            valid_to_at="2026-11-03T14:00:00+00:00",
            usage="MULTI_USE",
        )

    def create_conversion(self, **kwargs) -> Conversion:
        self.converted.append(kwargs)
        if self.fail:
            raise AirwallexError(400, self.fail, "")
        return Conversion(conversion_id="con_1", status="SETTLED", quote_id=kwargs["quote_id"])


def _proposed(conn, valid_to: str = VALID):
    incident_id = seed_hero(conn)
    lock_fx_quote(
        conn,
        quote_id="quo_1",
        pair="USD/EUR",
        rate="1.10",
        amount="4400.00",
        valid_to=valid_to,
    )
    terms = _terms()
    approval_id = propose_fx_quote(conn, incident_id, terms, proposer=AP_CLERK)
    ref = open_intent(
        conn,
        kind="fx_conversion",
        incident_id=incident_id,
        obligation_id=conn.execute("SELECT id FROM obligation").fetchone()["id"],
        action="FX_FUND",
        amount="4400.00",
        currency="EUR",
        beneficiary_id="ben_dkb",
        details_hash="hash-dkb",
        proposer=AP_CLERK,
        quote_id="quo_1",
        expires_at="2026-11-03T18:00:00+00:00",
        created_at=USED,
    )
    approve_grant(
        conn,
        ref.approval_id,
        intent_terms(conn, ref.id),
        approver=FINANCE_APPROVER,
        used_at=USED,
    )
    return terms, approval_id


def test_the_approval_hash_includes_the_quote_id():
    conn = connect()
    terms, approval_id = _proposed(conn)
    stored = conn.execute(
        "SELECT binding_hash FROM approval WHERE id = ?",
        (approval_id,),
    ).fetchone()
    assert stored["binding_hash"] == binding_hash(terms)
    other = _terms("quo_other")
    assert binding_hash(other) != stored["binding_hash"]

    with pytest.raises(ApprovalError) as caught:
        approve_fx_quote(
            conn,
            Desk(),
            approval_id,
            other,
            approver=FINANCE_APPROVER,
            used_at=USED,
            as_of=EARLY,
        )
    assert caught.value.code == "void"
    assert conn.execute("SELECT used_at FROM approval WHERE id = ?", (approval_id,)).fetchone()["used_at"] is None


def test_an_expired_quote_is_requoted_before_approval():
    conn = connect()
    terms, approval_id = _proposed(conn)
    desk = Desk()

    decision = approve_fx_quote(
        conn,
        desk,
        approval_id,
        terms,
        approver=FINANCE_APPROVER,
        used_at=USED,
        as_of=LATE,
    )

    assert decision.outcome == "reapprove"
    assert decision.quote_id == "quo_2"
    assert decision.approval_id != approval_id
    assert conn.execute("SELECT state FROM fx_quote WHERE quote_id = 'quo_1'").fetchone()["state"] == "EXPIRED"
    fresh = conn.execute("SELECT state FROM fx_quote WHERE quote_id = 'quo_2'").fetchone()
    assert fresh["state"] == "LOCKED"
    assert conn.execute("SELECT used_at FROM approval WHERE id = ?", (approval_id,)).fetchone()["used_at"] is None
    new_intent = conn.execute(
        "SELECT id FROM execution_intent WHERE quote_id = ?",
        (decision.quote_id,),
    ).fetchone()["id"]
    stored = conn.execute(
        "SELECT binding_hash FROM approval WHERE id = ?",
        (decision.approval_id,),
    ).fetchone()
    assert stored["binding_hash"] == binding_hash(intent_terms(conn, new_intent))
    assert desk.created[0]["buy_amount"] == Decimal("4400.00")
    assert desk.converted == []


def _execute_bound(conn, client, *, as_of: str, quote_id: str = "quo_1"):
    """Run the conversion with the execution intent's own approval, not a second grant."""
    row = conn.execute(
        """
        SELECT id, approval_id FROM execution_intent
        WHERE kind = 'fx_conversion' AND quote_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (quote_id,),
    ).fetchone()
    intent_id = int(row["id"])
    return execute_fx_quote(
        conn,
        client,
        int(row["approval_id"]),
        intent_terms(conn, intent_id),
        as_of=as_of,
        intent_id=intent_id,
    )


def test_an_expired_quote_is_requoted_again_before_execute():
    conn = connect()
    terms, approval_id = _proposed(conn)
    approve_fx_quote(
        conn,
        Desk(),
        approval_id,
        terms,
        approver=FINANCE_APPROVER,
        used_at=USED,
        as_of=EARLY,
    )
    desk = Desk()

    decision = _execute_bound(conn, desk, as_of=LATE)

    assert decision.outcome == "reapprove"
    assert decision.quote_id == "quo_2"
    assert desk.converted == []
    assert conn.execute("SELECT state FROM fx_quote WHERE quote_id = 'quo_1'").fetchone()["state"] == "EXPIRED"
    old = conn.execute("SELECT used_at FROM approval WHERE id = ?", (approval_id,)).fetchone()
    assert old["used_at"] == USED
    new = conn.execute(
        "SELECT used_at FROM approval WHERE id = ?",
        (decision.approval_id,),
    ).fetchone()
    assert new["used_at"] is None


def test_two_concurrent_claims_allow_one_conversion(tmp_path):
    path = tmp_path / "fx.db"
    conn = connect(path)
    migrate(conn)
    lock_fx_quote(
        conn,
        quote_id="quo_race",
        pair="USD/EUR",
        rate="1.10",
        amount="100.00",
        valid_to=VALID,
    )
    conn.commit()
    conn.close()
    barrier = threading.Barrier(2)
    sent: list[str] = []
    lost: list[str] = []

    def worker(name: str) -> None:
        local = connect(path)
        barrier.wait()
        try:
            consume_fx_quote(local, "quo_race", "")
            local.commit()
            sent.append(name)
        except LedgerError:
            lost.append(name)
        finally:
            local.close()

    threads = [threading.Thread(target=worker, args=(str(index),)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sent == ["1"] or sent == ["2"]
    assert len(sent) == 1
    assert len(lost) == 1
    stored = connect(path)
    row = stored.execute("SELECT state FROM fx_quote WHERE quote_id = 'quo_race'").fetchone()
    assert row["state"] == "CONSUMED"


def test_a_reused_quote_is_rejected_as_consumed():
    conn = connect()
    terms, approval_id = _proposed(conn)
    approve_fx_quote(
        conn,
        Desk(),
        approval_id,
        terms,
        approver=FINANCE_APPROVER,
        used_at=USED,
        as_of=EARLY,
    )
    desk = Desk()
    first = _execute_bound(conn, desk, as_of=EARLY)
    assert first.outcome == "converted"
    assert first.conversion_id == "con_1"

    second = _execute_bound(conn, desk, as_of=EARLY)
    assert second.outcome == "converted"
    assert second.conversion_id == "con_1"
    assert len(desk.converted) == 1
    assert conn.execute("SELECT state FROM fx_quote WHERE quote_id = 'quo_1'").fetchone()["state"] == "CONSUMED"


def test_a_consumed_quote_is_not_approved():
    conn = connect()
    terms, approval_id = _proposed(conn)
    consume_fx_quote(conn, "quo_1", "con_earlier")
    conn.commit()

    with pytest.raises(FxQuoteError) as caught:
        approve_fx_quote(
            conn,
            Desk(),
            approval_id,
            terms,
            approver=FINANCE_APPROVER,
            used_at=USED,
            as_of=EARLY,
        )
    assert caught.value.code == "consumed"


@pytest.mark.parametrize("code", ["expired_quote", "quote_expired"])
def test_an_airwallex_expiry_code_requotes_and_asks_for_a_new_approval(code):
    conn = connect()
    terms, approval_id = _proposed(conn)
    approve_fx_quote(
        conn,
        Desk(),
        approval_id,
        terms,
        approver=FINANCE_APPROVER,
        used_at=USED,
        as_of=EARLY,
    )
    desk = Desk(fail=code)

    decision = _execute_bound(conn, desk, as_of=EARLY)

    assert decision.outcome == "reapprove"
    assert decision.quote_id == "quo_2"
    assert conn.execute("SELECT state FROM fx_quote WHERE quote_id = 'quo_1'").fetchone()["state"] == "EXPIRED"
    assert len(desk.converted) == 1


def test_the_replacement_quote_converts_after_its_own_approval():
    conn = connect()
    terms, approval_id = _proposed(conn)
    desk = Desk()
    decision = approve_fx_quote(
        conn,
        desk,
        approval_id,
        terms,
        approver=FINANCE_APPROVER,
        used_at=USED,
        as_of=LATE,
    )
    assert decision.outcome == "reapprove"
    new_id = conn.execute(
        "SELECT id FROM execution_intent WHERE quote_id = ?",
        (decision.quote_id,),
    ).fetchone()["id"]
    old_id = conn.execute(
        "SELECT id FROM execution_intent WHERE quote_id = 'quo_1'"
    ).fetchone()["id"]
    assert new_id != old_id
    approve_grant(
        conn,
        decision.approval_id,
        intent_terms(conn, new_id),
        approver=FINANCE_APPROVER,
        used_at=USED,
    )

    converted = execute(conn, desk, new_id, as_of=EARLY)
    again = execute(conn, desk, new_id, as_of=EARLY)

    assert converted.remote_id == "con_1"
    assert again.remote_id == "con_1"
    assert len(desk.converted) == 1


def _locked_for(conn, *, quote_amount: str, intent_amount: str, currency: str, pair: str = "USD/EUR"):
    incident_id = seed_hero(conn)
    obligation_id = conn.execute("SELECT id FROM obligation").fetchone()["id"]
    lock_fx_quote(
        conn,
        quote_id="quo_gap",
        pair=pair,
        rate="1.10",
        amount=quote_amount,
        valid_to=VALID,
    )
    conn.commit()
    ref = open_intent(
        conn,
        kind="fx_conversion",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="FX_FUND",
        amount=intent_amount,
        currency=currency,
        beneficiary_id="ben_dkb",
        details_hash="hash-dkb",
        proposer=AP_CLERK,
        quote_id="quo_gap",
        expires_at="2026-11-03T18:00:00+00:00",
        created_at=USED,
    )
    approve_grant(
        conn,
        ref.approval_id,
        intent_terms(conn, ref.id),
        approver=FINANCE_APPROVER,
        used_at=USED,
    )
    return ref


def test_an_fx_intent_for_a_different_amount_does_not_convert():
    conn = connect()
    ref = _locked_for(conn, quote_amount="100.00", intent_amount="1.00", currency="EUR")
    desk = Desk()

    with pytest.raises(ExecuteError) as caught:
        execute(conn, desk, ref.id, as_of=EARLY)

    assert caught.value.code == "void"
    assert desk.converted == []
    intent = conn.execute("SELECT state FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
    assert intent["state"] == "proposed"
    quote = conn.execute("SELECT state FROM fx_quote WHERE quote_id = 'quo_gap'").fetchone()
    assert quote["state"] == "LOCKED"


def test_an_fx_intent_for_a_different_currency_does_not_convert():
    conn = connect()
    ref = _locked_for(conn, quote_amount="100.00", intent_amount="100.00", currency="USD")
    desk = Desk()

    with pytest.raises(ExecuteError) as caught:
        execute(conn, desk, ref.id, as_of=EARLY)

    assert caught.value.code == "void"
    assert desk.converted == []
    intent = conn.execute("SELECT state FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
    assert intent["state"] == "proposed"
    quote = conn.execute("SELECT state FROM fx_quote WHERE quote_id = 'quo_gap'").fetchone()
    assert quote["state"] == "LOCKED"


def test_an_interrupted_conversion_is_recovered_by_request_id():
    conn = connect()
    terms, _approval_id = _proposed(conn)
    desk = Desk()
    found: dict[str, Conversion] = {}

    def create_conversion(**kwargs) -> Conversion:
        desk.converted.append(kwargs)
        conversion = Conversion(conversion_id="con_crash", status="SETTLED", quote_id=kwargs["quote_id"])
        found[kwargs["request_id"]] = conversion
        raise RuntimeError("crash")

    def find_conversion(request_id: str) -> Conversion | None:
        return found.get(request_id)

    desk.create_conversion = create_conversion
    desk.find_conversion = find_conversion
    intent_id = int(conn.execute("SELECT id FROM execution_intent WHERE quote_id = 'quo_1'").fetchone()["id"])

    with pytest.raises(RuntimeError, match="crash"):
        execute(conn, desk, intent_id, as_of=EARLY)

    pending = conn.execute(
        "SELECT state, request_id, remote_id FROM execution_intent WHERE id = ?",
        (intent_id,),
    ).fetchone()
    assert pending["state"] == "executed"
    assert pending["remote_id"] is None
    assert pending["request_id"] == desk.converted[0]["request_id"]
    assert conn.execute("SELECT consumed_by FROM fx_quote WHERE quote_id = 'quo_1'").fetchone()["consumed_by"] == ""

    recovered = execute(conn, desk, intent_id, as_of=EARLY)

    assert recovered.remote_id == "con_crash"
    assert len(desk.converted) == 1
    assert conn.execute("SELECT consumed_by FROM fx_quote WHERE quote_id = 'quo_1'").fetchone()["consumed_by"] == "con_crash"
    assert terms.quote_id == "quo_1"

"""Replacement unlocks only on a full, posted, matching refund. Reached through execute(). No network."""

from dataclasses import replace
from decimal import Decimal

import pytest
from test_execute import SEEN, Pay, _grant, _ready

from backend.airwallex.types import (
    BalanceHistoryItem,
    BalanceHistoryPage,
    Transfer,
    parse_history_item,
)
from backend.execute import execute
from backend.ledger import connect, migrate
from backend.outcomes import HistoryError, full_refund, original_returned, reversal_for
from backend.reconcile import _read_outcome
from backend.replace_payment import ReplaceError


def _item(id="rev", *, source="awx-hero", amount="4200.00", currency="EUR", kind="PAYOUT_REVERSAL", cls=BalanceHistoryItem, account_type="CASH", **extra):
    return cls(
        id=id,
        source=source,
        amount=Decimal(amount),
        currency=currency,
        balance=None,
        description=None,
        fee=Decimal(0),
        posted_at=None,
        source_type="PAYOUT",
        transaction_type=kind,
        account_type=account_type,
        reference="INV-2291",
        **extra,
    )


class Book(Pay):
    """pages: cursor -> BalanceHistoryPage. The first request has no cursor."""

    def __init__(self, items=None, *, pages=None, source_amount=None, source_currency=None, **kwargs):
        super().__init__(**kwargs)
        self.pages = pages if pages is not None else {None: BalanceHistoryPage(tuple(items or ()), False, None)}
        self.source_amount = source_amount
        self.source_currency = source_currency
        self.history_calls = 0

    def get_transfer(self, transfer_id):
        base = super().get_transfer(transfer_id)
        return Transfer(
            **{**base.__dict__, "source_amount": self.source_amount, "source_currency": self.source_currency}
        )

    def balance_history(self, *, page=None, **_kwargs):
        self.history_calls += 1
        return self.pages[page]


def _refused(items=None, *, expect="refund", **kwargs):
    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    client = Book(items, **kwargs)
    ref = _grant(conn, incident_id)
    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)
    assert caught.value.code == expect
    assert client.created == [] and client.conversions == []
    row = conn.execute("SELECT state, request_id FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
    assert row["state"] != "executed" and row["request_id"] is None


def _pays(items=None, **kwargs):
    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    client = Book(items, **kwargs)
    ref = _grant(conn, incident_id)
    assert execute(conn, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert len(client.created) == 1


def test_a_full_matching_refund_pays_once():
    _pays([_item()])


def test_an_explicit_same_currency_source_is_the_principal():
    _pays([_item()], source_amount=Decimal("4200.00"), source_currency="EUR")
    _refused([_item(amount="4200.00")], source_amount=Decimal("4213.47"), source_currency="EUR")


def test_a_partial_refund_does_not_unlock():
    _refused([_item(amount="2000.00")])


def test_a_wrong_currency_refund_does_not_unlock():
    _refused([_item(currency="USD")])


def test_a_refund_for_another_transfer_does_not_unlock():
    _refused([_item(source="awx-other")])


@pytest.mark.parametrize("amount", ["-4200.00", "0", "NaN", "Infinity"])
def test_only_a_positive_finite_credit_unlocks(amount):
    _refused([_item(amount=amount)])


def test_a_debit_is_not_a_refund_of_the_same_size():
    _refused([_item(amount="-4200.00")])


def test_a_changed_live_amount_changes_the_required_refund():
    _refused([_item(amount="4200.00")], amount="5000.00")


def test_cross_currency_principal_without_a_source_amount_holds():
    _refused([_item()], source_currency="USD")


def test_cross_currency_needs_the_source_amount_and_currency():
    usd = {"source_amount": Decimal("4620.00"), "source_currency": "USD"}
    _refused([_item(amount="4200.00", currency="EUR")], **usd)
    _pays([_item(amount="4620.00", currency="USD")], **usd)


def test_a_pending_refund_does_not_unlock():
    _refused([_item(status="PENDING")])
    _pays([_item(status="SETTLED")])


def test_two_credits_with_one_history_id_do_not_unlock():
    _refused([_item(), _item()])


def test_two_distinct_credits_for_one_transfer_do_not_unlock():
    _refused([_item("a"), _item("b")])


def _pages(last: dict | None = None, *, count: int = 7):
    noise = lambda n: tuple(_item(f"x{n}-{i}", source=f"awx-{n}-{i}") for i in range(10))
    pages = {None: BalanceHistoryPage(noise(0), True, "p1")}
    for n in range(1, count):
        pages[f"p{n}"] = BalanceHistoryPage(noise(n), True, f"p{n + 1}")
    pages[f"p{count}"] = BalanceHistoryPage((_item("late"),), False, None)
    pages.update(last or {})
    return pages


def test_a_refund_after_page_five_unlocks():
    _pays(pages=_pages())


def test_a_repeated_cursor_fails_closed():
    pages = _pages({"p3": BalanceHistoryPage((), True, "p2")})
    _refused(pages=pages)


def test_a_missing_cursor_fails_closed():
    pages = _pages({"p3": BalanceHistoryPage((), True, None)})
    _refused(pages=pages)


def test_a_refund_that_arrives_after_restart_unlocks_once(tmp_path):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    migrate(conn)
    _obligation_id, incident_id = _ready(conn)
    ref = _grant(conn, incident_id)
    first = Book([_item(amount="2000.00")])
    with pytest.raises(ReplaceError):
        execute(conn, first, ref.id, as_of=SEEN)
    conn.close()

    conn = connect(path)
    second = Book([_item()])
    assert execute(conn, second, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert first.created == [] and len(second.created) == 1


def test_a_changed_refund_after_restart_locks_again(tmp_path):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    migrate(conn)
    _obligation_id, incident_id = _ready(conn)
    ref = _grant(conn, incident_id)
    conn.close()
    conn = connect(path)
    client = Book([_item(amount="4199.99")])
    with pytest.raises(ReplaceError):
        execute(conn, client, ref.id, as_of=SEEN)
    assert client.created == []


def test_a_partial_reversal_is_still_detected_but_is_not_a_return():
    conn = connect()
    _ready(conn)
    client = Book([_item(amount="2000.00")])
    assert reversal_for(client, "awx-hero", "EUR").amount == Decimal("2000.00")
    original = conn.execute("SELECT * FROM transfer_record WHERE airwallex_id = 'awx-hero'").fetchone()
    assert original_returned(client, original) is False
    assert original_returned(Book([_item()]), original) is True


def test_reconcile_does_not_account_for_the_original_on_a_partial_reversal():
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    row = conn.execute("SELECT * FROM transfer_record WHERE airwallex_id = 'awx-hero'").fetchone()
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    partial, _ = _read_outcome(conn, Book([_item(amount="2000.00")]), incident_id, obligation, row, {}, True, SEEN)
    assert partial.reversed is True and partial.returned is False
    full, _ = _read_outcome(conn, Book([_item()]), incident_id, obligation, row, {}, True, SEEN)
    assert full.reversed is True and full.returned is True


def test_history_errors_are_explicit():
    with pytest.raises(HistoryError):
        reversal_for(Book(pages={None: BalanceHistoryPage((), True, None)}), "awx-hero", "EUR")


# --- review 1: real parser, malformed evidence, bound durable verification ---


def _wire_raw(status="__absent__", *, amount="4200.00", currency="EUR", id="rev", source="awx-hero", kind="PAYOUT_REVERSAL", account_type="__absent__"):
    """A history row as the endpoint returns it, parsed by the real adapter parser."""
    data = {"id": id, "source": source, "amount": amount, "currency": currency, "transaction_type": kind}
    if status != "__absent__":
        data["status"] = status
    if account_type != "__absent__":
        data["account_type"] = account_type
    return parse_history_item(data)


def _wire(status="__absent__", **kw):
    """Like _wire_raw, but a CASH credit unless the test says otherwise."""
    kw.setdefault("account_type", "CASH")
    return _wire_raw(status, **kw)


def test_the_parser_keeps_the_status_the_endpoint_returned():
    assert _wire("PENDING").status == "PENDING"
    assert _wire("POSTED").status == "POSTED"
    assert _wire().status is None


def test_a_pending_wire_refund_does_not_unlock_through_execute():
    _refused([_wire("PENDING")])
    _refused([_wire("SOMETHING_NEW")])  # unknown explicit status holds
    _refused([_wire("")])


def test_a_posted_wire_refund_unlocks_and_so_does_one_without_status():
    # Endpoint contract: balance history is the posted ledger, so a row without a status is posted.
    _pays([_wire("POSTED")])
    _pays([_wire()])


def test_a_source_currency_full_refund_unlocks_through_execute_with_the_parser():
    usd = {"source_amount": Decimal("4620.00"), "source_currency": "USD"}
    _pays([_wire("POSTED", amount="4620.00", currency="USD")], **usd)
    _refused([_wire("POSTED", amount="4200.00", currency="EUR")], **usd)
    _refused([_wire("PENDING", amount="4620.00", currency="USD")], **usd)
    _refused([_wire("POSTED", amount="4619.99", currency="USD")], **usd)


def _remote(**kwargs):
    fields = {"id": "awx-hero", "status": "CANCELLED", "transfer_amount": Decimal("4200.00"), "transfer_currency": "EUR"}
    return Transfer(**{**fields, **kwargs})


def test_full_refund_validates_the_item_itself():
    assert full_refund(_remote(), [_wire()]) is not None
    assert full_refund(_remote(), [_wire(kind="PAYOUT_FEE")]) is None
    assert full_refund(_remote(), [_wire(source="awx-other")]) is None
    assert full_refund(_remote(), [_wire(source=None)]) is None
    assert full_refund(_remote(id="awx-other"), [_wire()]) is None


@pytest.mark.parametrize("field, value", [("amount", "garbage"), ("amount", None), ("currency", None), ("amount", object())])
def test_malformed_evidence_fails_closed_without_raising(field, value):
    bad = replace(_item(), **{field: value})
    assert full_refund(_remote(), [bad]) is None


def test_malformed_principal_fails_closed_without_raising():
    assert full_refund(_remote(transfer_amount="garbage"), [_wire()]) is None


class _Down(Book):
    def balance_history(self, **_kwargs):
        raise OSError("history unavailable")


def _file_ledger(tmp_path):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    migrate(conn)
    obligation_id, incident_id = _ready(conn)
    conn.close()
    return path, obligation_id, incident_id


def _outcome(path, client, obligation_id, incident_id):
    conn = connect(path)  # a fresh connection each time: nothing lives in memory
    row = conn.execute("SELECT * FROM transfer_record WHERE airwallex_id = 'awx-hero'").fetchone()
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    result, readable = _read_outcome(conn, client, incident_id, obligation, row, {}, True, SEEN)
    conn.commit()  # as the poller does; the next call reopens the file and sees only what was committed
    conn.close()
    return result, readable


def test_a_verified_full_return_survives_restart_and_a_pure_transport_failure(tmp_path):
    path, obligation_id, incident_id = _file_ledger(tmp_path)
    full, _ = _outcome(path, Book([_item()]), obligation_id, incident_id)
    assert full.returned is True
    down, readable = _outcome(path, _Down([]), obligation_id, incident_id)
    assert readable is False and down.returned is True and down.reversed is True


def test_fresh_contradictory_history_withdraws_an_earlier_full_return(tmp_path):
    path, obligation_id, incident_id = _file_ledger(tmp_path)
    assert _outcome(path, Book([_item()]), obligation_id, incident_id)[0].returned is True
    contradictions = (
        [_item(amount="2000.00")],
        [_item(currency="USD")],
        [_item(status="PENDING")],
        [_item("a"), _item("b")],
    )
    for items in contradictions:
        out, _ = _outcome(path, Book(items), obligation_id, incident_id)
        assert out.returned is False and out.reversed is True, items
    # the withdrawal is not sticky once the full credit is read again
    assert _outcome(path, Book([_item()]), obligation_id, incident_id)[0].returned is True


def test_an_earlier_verification_is_bound_to_the_principal(tmp_path):
    path, obligation_id, incident_id = _file_ledger(tmp_path)
    assert _outcome(path, Book([_item()]), obligation_id, incident_id)[0].returned is True
    # The transfer now reads as a different principal while history is unreachable.
    out, _ = _outcome(path, _Down([], amount="5000.00"), obligation_id, incident_id)
    assert out.returned is False and out.reversed is True
    other = _Down([], source_amount=Decimal("4620.00"), source_currency="USD")
    assert _outcome(path, other, obligation_id, incident_id)[0].returned is False
    assert _outcome(path, _Down([]), obligation_id, incident_id)[0].returned is True


# --- PR43 item 3: only a CASH credit is a refund ---

_NONCASH = ["CREDIT", "YIELD", "credit", "yield", "", "  ", "SAVINGS", None]


@pytest.mark.parametrize("kind", ["CASH", "cash", " Cash "])
def test_a_cash_credit_in_any_case_unlocks(kind):
    _pays([_item(account_type=kind)])
    _pays([_wire(account_type=kind)])


@pytest.mark.parametrize("kind", _NONCASH)
def test_a_noncash_or_unknown_account_type_never_unlocks_even_with_cash_available(kind):
    _refused([_item(account_type=kind)])
    _refused([_wire_raw() if kind is None else _wire_raw(account_type=kind)])


def test_the_parser_never_defaults_a_missing_account_type_to_cash():
    assert _wire_raw().account_type is None
    assert (_wire_raw(account_type="").account_type or "").strip().lower() != "cash"
    _refused([_wire_raw()])


@pytest.mark.parametrize("kind", ["CREDIT", "YIELD", None, ""])
def test_a_noncash_reversal_is_not_a_return_but_is_still_a_reversal(kind):
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    row = conn.execute("SELECT * FROM transfer_record WHERE airwallex_id = 'awx-hero'").fetchone()
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    client = Book([_item(account_type=kind)])
    assert reversal_for(client, "awx-hero", "EUR") is not None
    assert original_returned(client, row) is False
    out, _ = _read_outcome(conn, client, incident_id, obligation, row, {}, True, SEEN)
    assert out.reversed is True and out.returned is False


def test_cash_then_noncash_then_cash_across_restart_pays_once(tmp_path):
    path, obligation_id, incident_id = _file_ledger(tmp_path)
    assert _outcome(path, Book([_item()]), obligation_id, incident_id)[0].returned is True
    out, _ = _outcome(path, Book([_item(account_type="YIELD")]), obligation_id, incident_id)
    assert out.returned is False and out.reversed is True
    conn = connect(path)
    ref = _grant(conn, incident_id)
    held = Book([_item(account_type="YIELD")])
    with pytest.raises(ReplaceError) as caught:
        execute(conn, held, ref.id, as_of=SEEN)
    assert caught.value.code == "refund"
    assert held.created == [] and held.conversions == []
    assert conn.execute("SELECT state FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()["state"] != "executed"
    conn.close()

    conn = connect(path)
    cash = Book([_item()])
    assert execute(conn, cash, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert len(cash.created) == 1 and cash.conversions == []

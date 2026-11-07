"""A new payout needs a full refund and live funding first. Reached through execute(). No network."""

import json
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_execute import SEEN, Pay, _grant, _ready

from backend.airwallex.types import (
    Balance,
    BalanceHistoryItem,
    BalanceHistoryPage,
    Transfer,
    TransferPage,
)
from backend.execute import execute
from backend.ledger import connect, migrate
from backend.replace_payment import ReplaceError, replace_payment

NEED = Decimal("5226.94")  # 4200.00 + fee budget 26.94 (2 x 13.47 original fee) + 1000.00 default floor


@pytest.fixture(autouse=True)
def default_floor(monkeypatch):
    monkeypatch.delenv("SETTLEWELL_RESERVE_FLOOR", raising=False)


def rows(available="10000.00", *, pending="0", currency="EUR", total=None):
    value = Decimal(available)
    return [
        Balance(currency, value, Decimal(pending), Decimal(0), Decimal(total) if total else value, Decimal(0), account_type="cash"),
        Balance("USD", Decimal("100000.00"), Decimal(0), Decimal(0), Decimal("100000.00"), Decimal(0), account_type="cash"),
    ]


class Funded(Pay):
    """Balance reads follow a script; the last entry repeats. An Exception entry is raised."""

    def __init__(self, *, balances=None, fee="13.47", fee_currency="EUR", actual_fee=None, refund=True, crash=False):
        super().__init__()
        self.script = list(balances) if balances else [rows()]
        self.fee = None if fee is None else Decimal(fee)
        self.fee_currency = fee_currency
        self.actual_fee = actual_fee
        self.refund = refund
        self.crash = crash
        self.originals = {"awx-hero": "4200.00", "awx-hero2": "4200.00"}
        self.accepted: dict[str, Transfer] = {}
        self.balance_calls = 0

    def get_transfer(self, transfer_id):
        if transfer_id in self.accepted:
            return self.accepted[transfer_id]
        return Transfer(
            id=transfer_id,
            status="CANCELLED",
            request_id="original-payment",
            transfer_amount=Decimal(self.originals[transfer_id]),
            transfer_currency="EUR",
            transfer_method="SWIFT",
            fee_amount=self.fee,
            fee_currency=self.fee_currency,
            failure_reason=self.reason,
            reference="INV-2291",
        )

    def balance_history(self, **_kwargs):
        items = ()
        if self.refund:
            items = tuple(
                BalanceHistoryItem(
                    id=f"rev-{source}", source=source, amount=Decimal(amount), currency="EUR", balance=None,
                    description=None, fee=Decimal(0), posted_at=None, source_type="PAYOUT",
                    transaction_type="PAYOUT_REVERSAL", account_type="CASH", reference="INV-2291",
                )
                for source, amount in self.originals.items()
            )
        return BalanceHistoryPage(items=items, has_more=False, page_after=None)

    def current_balances(self):
        step = self.script[min(self.balance_calls, len(self.script) - 1)]
        self.balance_calls += 1
        if isinstance(step, Exception):
            raise step
        return step

    def create_transfer(self, **kwargs):
        self.created.append(kwargs)
        new = Transfer(
            id="awx-new" if len(self.created) == 1 else f"awx-new-{len(self.created)}",
            status="SCHEDULED",
            request_id=kwargs["request_id"],
            transfer_amount=kwargs["transfer_amount"],
            transfer_currency=kwargs["transfer_currency"],
            fee_amount=Decimal(self.actual_fee) if self.actual_fee is not None else None,
            fee_currency="EUR" if self.actual_fee is not None else None,
            beneficiary_id=kwargs["beneficiary_id"],
            reference=kwargs["reference"],
        )
        self.accepted[new.id] = new
        if self.crash:
            raise RuntimeError("process died after the bank accepted")
        return new

    def list_transfers(self, **kwargs):
        wanted = kwargs.get("request_id")
        return TransferPage(
            items=tuple(item for item in self.accepted.values() if item.request_id == wanted), page_after=None
        )


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _setup(**kwargs):
    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    return conn, Funded(**kwargs), _grant(conn, incident_id)


def _refused(conn, client, ref, code):
    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)
    assert caught.value.code == code
    assert client.created == [] and client.conversions == []
    row = conn.execute("SELECT state, request_id FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
    assert row["state"] == "proposed" and row["request_id"] is None
    assert _count(conn, "payout_funding") == 0
    assert conn.execute("SELECT used_at FROM approval WHERE id = ?", (ref.approval_id,)).fetchone()["used_at"]


def test_exactly_the_threshold_pays_and_records_the_preflight():
    conn, client, ref = _setup(balances=[rows(str(NEED)), rows("777.77")])
    result = execute(conn, client, ref.id, as_of=SEEN)
    assert result.remote_id == "awx-new" and len(client.created) == 1
    row = conn.execute("SELECT * FROM payout_funding").fetchone()
    assert (row["amount"], row["currency"], row["fee_budget"], row["fee_currency"]) == ("4200.00", "EUR", "26.94", "EUR")
    assert row["fee_source"] == "conservative_upper_bound_estimate"
    assert (row["reserve_floor"], row["available_before"]) == ("1000.00", str(NEED))
    assert row["request_id"] == client.created[0]["request_id"]
    assert row["intent_id"] == ref.id and row["incident_id"] and row["obligation_id"]
    snaps = conn.execute("SELECT label, available, request_id FROM balance_snapshot ORDER BY id").fetchall()
    assert [(s["label"], s["available"], s["request_id"]) for s in snaps] == [
        ("before", str(NEED), row["request_id"]),
        ("after", "777.77", row["request_id"]),  # the live read, not before minus amount
    ]
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM audit_event")}
    assert {"payout_funding_preflight", "payout_after_observed"} <= kinds
    assert "cannot guarantee" in result.funding_note and "conservative_upper_bound_estimate" in result.funding_note


@pytest.mark.parametrize(
    ("available", "env_floor"),
    [
        (str(NEED - Decimal("0.01")), None),  # one cent short
        ("5200.00", None),  # covers amount, not fee estimate plus floor
        ("5213.47", None),  # the old amount + 13.47 + floor is no longer enough
        (str(NEED), "1000.01"),  # floor raised
    ],
)
def test_a_shortfall_never_pays(available, env_floor, monkeypatch):
    if env_floor:
        monkeypatch.setenv("SETTLEWELL_RESERVE_FLOOR", env_floor)
    conn, client, ref = _setup(balances=[rows(available)])
    _refused(conn, client, ref, "balance")


def test_pending_prepaid_and_total_do_not_count_as_available():
    pending = [
        Balance("EUR", Decimal(0), Decimal(99999), Decimal(99999), Decimal(99999), Decimal(99999), account_type="cash"),
    ]
    conn, client, ref = _setup(balances=[pending])
    _refused(conn, client, ref, "balance")


@pytest.mark.parametrize(
    "script",
    [
        [[Balance("USD", Decimal(9999), Decimal(0), Decimal(0), Decimal(9999), Decimal(0), account_type="cash")]],  # no EUR row
        [TimeoutError("slow")],
        [OSError("down")],
        [rows("NaN")],
        [rows("Infinity")],
        [rows("-5.00")],
        [rows("10000.00") + rows("10000.00")[:1]],  # duplicate EUR rows
    ],
)
def test_an_unknown_or_malformed_balance_never_pays(script):
    conn, client, ref = _setup(balances=script)
    _refused(conn, client, ref, "balance")


@pytest.mark.parametrize(
    ("fee", "fee_currency"),
    [(None, "EUR"), ("NaN", "EUR"), ("Infinity", "EUR"), ("-1.00", "EUR"), ("13.47", "USD")],
)
def test_a_missing_or_unusable_fee_estimate_never_pays(fee, fee_currency):
    conn, client, ref = _setup(fee=fee, fee_currency=fee_currency)
    _refused(conn, client, ref, "fee")


@pytest.mark.parametrize("floor", ["nan", "inf", "-5", "0", "abc"])
def test_a_bad_reserve_floor_never_pays(floor, monkeypatch):
    monkeypatch.setenv("SETTLEWELL_RESERVE_FLOOR", floor)
    conn, client, ref = _setup()
    _refused(conn, client, ref, "floor")


def test_a_refused_preflight_leaves_the_approval_usable():
    conn, client, ref = _setup(balances=[rows("100.00"), rows("10000.00")])
    with pytest.raises(ReplaceError):
        execute(conn, client, ref.id, as_of=SEEN)
    assert execute(conn, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert len(client.created) == 1


def _direct(conn, client, ref, **overrides):
    obligation_id = conn.execute("SELECT obligation_id FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()[0]
    args = {
        "obligation_id": obligation_id, "attempt": 1, "beneficiary_id": "ben_verified",
        "details_hash": "callback-v2", "transfer_method": "SWIFT", "reason": "other_services",
        "as_of": SEEN, "intent_id": ref.id, "approval_id": ref.approval_id,
    }
    return replace_payment(conn, client, **{**args, **overrides})


def test_a_direct_helper_call_with_a_grant_still_needs_the_refund():
    conn, client, ref = _setup(refund=False)
    with pytest.raises(ReplaceError) as caught:
        _direct(conn, client, ref, transfer_amount=Decimal("4200.00"), transfer_currency="EUR")
    assert caught.value.code == "refund"
    assert client.created == []
    assert conn.execute("SELECT state FROM execution_intent").fetchone()[0] == "proposed"


def test_a_direct_helper_call_cannot_pay_a_stale_local_amount():
    conn, client, ref = _setup()
    conn.execute("UPDATE obligation SET amount = '4100.00'")  # local obligation differs from the live original
    conn.execute("UPDATE execution_intent SET amount = '4100.00'")
    conn.commit()
    client.originals["awx-hero"] = "4200.00"
    with pytest.raises(ReplaceError) as caught:
        _direct(conn, client, ref)  # no explicit amount: falls back to the obligation
    assert caught.value.code in {"void", "approval"}
    assert client.created == []
    assert _count(conn, "payout_funding") == 0


def test_an_accepted_send_recovers_without_refund_or_funding_after_restart(tmp_path):
    path = tmp_path / "recover.db"
    setup = connect(path)
    migrate(setup)
    _obligation_id, incident_id = _ready(setup)
    ref = _grant(setup, incident_id)
    client = Funded(crash=True)
    with pytest.raises(RuntimeError):
        execute(setup, client, ref.id, as_of=SEEN)
    setup.close()
    assert len(client.created) == 1

    client.crash = False
    client.refund = False  # refund history gone
    client.script = [rows("1.00")]  # and the balance is now far too low for a new payout
    restarted = connect(path)
    result = execute(restarted, client, ref.id, as_of=SEEN)
    assert result.remote_id == "awx-new"
    assert len(client.created) == 1  # found by request id, never re-created
    assert _count(restarted, "payout_funding") == 1
    after = restarted.execute("SELECT available FROM balance_snapshot WHERE label = 'after'").fetchall()
    assert [r["available"] for r in after] == ["1.00"]


def test_an_after_read_failure_keeps_the_send_and_a_restart_backfills_once(tmp_path):
    path = tmp_path / "after.db"
    setup = connect(path)
    migrate(setup)
    _obligation_id, incident_id = _ready(setup)
    ref = _grant(setup, incident_id)
    client = Funded(balances=[rows(), TimeoutError("after read"), rows("5000.00")])
    assert execute(setup, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    state = setup.execute("SELECT after_state FROM payout_funding").fetchone()[0]
    assert state == "MISSING"
    assert setup.execute("SELECT COUNT(*) FROM audit_event WHERE kind = 'payout_after_missing'").fetchone()[0] == 1
    assert setup.execute("SELECT remote_id FROM execution_intent").fetchone()[0] == "awx-new"
    setup.close()

    restarted = connect(path)
    assert execute(restarted, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert execute(restarted, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert len(client.created) == 1
    assert restarted.execute("SELECT after_state FROM payout_funding").fetchone()[0] == "OBSERVED"
    after = restarted.execute("SELECT available FROM balance_snapshot WHERE label = 'after'").fetchall()
    assert [r["available"] for r in after] == ["5000.00"]


def test_an_actual_fee_above_the_old_fee_but_inside_the_budget_pays_without_escalation():
    conn, client, ref = _setup(actual_fee="20.00")
    assert execute(conn, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    row = conn.execute("SELECT actual_fee, fee_budget FROM payout_funding").fetchone()
    assert (row["actual_fee"], row["fee_budget"]) == ("20.00", "26.94")
    assert conn.execute("SELECT COUNT(*) FROM audit_event WHERE kind = 'payout_fee_over_budget'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM obligation WHERE invoice_ref = 'INV-2291'").fetchone()[0] != "ESCALATED"


def test_a_zero_original_fee_still_needs_the_25_minimum_budget():
    need = Decimal("4200.00") + Decimal("25.00") + Decimal("1000.00")
    conn, client, ref = _setup(fee="0", balances=[rows(str(need - Decimal("0.01")))])
    _refused(conn, client, ref, "balance")
    conn, client, ref = _setup(fee="0", balances=[rows(str(need))])
    execute(conn, client, ref.id, as_of=SEEN)
    assert conn.execute("SELECT fee_budget FROM payout_funding").fetchone()[0] == "25.00"


def test_a_large_original_fee_doubles():
    conn, client, ref = _setup(fee="40.00", balances=[rows("5280.00")])  # 4200 + 80 + 1000
    execute(conn, client, ref.id, as_of=SEEN)
    assert conn.execute("SELECT fee_budget FROM payout_funding").fetchone()[0] == "80.00"


def test_an_unsupported_currency_has_no_fallback_budget():
    from backend.funding import _fee_estimate

    remote = Transfer(
        id="x", status="CANCELLED", request_id="r", transfer_amount=Decimal(1),
        transfer_currency="JPY", fee_amount=Decimal(1), fee_currency="JPY",
    )
    assert _fee_estimate(remote, "JPY") is None


def _quote(client, **overrides):
    def quoter(**kwargs):
        fields = {
            "fee_amount": Decimal("30.00"), "fee_currency": "EUR", "beneficiary_id": kwargs["beneficiary_id"],
            "transfer_amount": kwargs["transfer_amount"], "transfer_currency": kwargs["transfer_currency"],
            "transfer_method": kwargs["transfer_method"], "valid_to_at": "2999-01-01T00:00:00+00:00",
            "source": "test-offline-quote",
        }
        fields.update(overrides)
        client.quote_calls.append(kwargs)
        return SimpleNamespace(**fields)

    client.quote_calls = []
    client.quote_payout_fee = quoter


def test_a_valid_quote_for_the_exact_new_payout_overrides_the_fallback():
    conn, client, ref = _setup(balances=[rows("5230.00")])  # 4200 + quoted 30 + 1000
    _quote(client)
    execute(conn, client, ref.id, as_of=SEEN)
    row = conn.execute("SELECT fee_budget, fee_source FROM payout_funding").fetchone()
    assert (row["fee_budget"], row["fee_source"]) == ("30.00", "adapter_fee_quote")
    call = client.quote_calls[0]
    assert (call["transfer_amount"], call["transfer_currency"], call["transfer_method"]) == (
        Decimal("4200.00"), "EUR", client.created[0]["transfer_method"],
    )
    assert call["beneficiary_id"] == client.created[0]["beneficiary_id"]


def test_a_quote_the_balance_cannot_cover_holds():
    conn, client, ref = _setup(balances=[rows("5229.99")])
    _quote(client)
    _refused(conn, client, ref, "balance")


@pytest.mark.parametrize(
    "override",
    [
        {"beneficiary_id": "ben-other"},
        {"transfer_amount": Decimal("4200.01")},
        {"transfer_currency": "USD"},
        {"transfer_method": "LOCAL"},
        {"fee_currency": "USD"},
        {"fee_amount": None},
        {"fee_amount": Decimal(-1)},
        {"valid_to_at": "2000-01-01T00:00:00+00:00"},  # stale
        {"valid_to_at": "garbage"},
        {"source": ""},
    ],
)
def test_a_wrong_stale_or_malformed_quote_holds_and_never_falls_back(override):
    conn, client, ref = _setup()
    _quote(client, **override)
    _refused(conn, client, ref, "fee")


def test_a_quote_that_errors_holds_and_never_falls_back():
    conn, client, ref = _setup()

    def broken(**_kwargs):
        raise TimeoutError("slow")

    client.quote_payout_fee = broken
    _refused(conn, client, ref, "fee")


def test_an_actual_fee_over_budget_is_audited_and_never_resent():
    conn, client, ref = _setup(actual_fee="40.00")
    assert execute(conn, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    row = conn.execute("SELECT actual_fee, actual_fee_currency FROM payout_funding").fetchone()
    assert (row["actual_fee"], row["actual_fee_currency"]) == ("40.00", "EUR")
    detail = conn.execute("SELECT detail FROM audit_event WHERE kind = 'payout_fee_over_budget'").fetchone()
    assert detail and json.loads(detail["detail"])["budget"] == "26.94"
    audit = conn.execute("SELECT detail FROM audit_event WHERE kind = 'payout_funding_preflight'").fetchone()
    assert json.loads(audit["detail"])["fee_policy"].startswith("max(2 x live original fee")
    assert conn.execute("SELECT status FROM obligation WHERE invoice_ref = 'INV-2291'").fetchone()[0] == "ESCALATED"
    assert conn.execute("SELECT COUNT(*) FROM audit_event WHERE kind = 'payout_escalated'").fetchone()[0] == 1
    execute(conn, client, ref.id, as_of=SEEN)
    assert len(client.created) == 1


def test_two_workers_for_one_intent_pay_and_record_once(tmp_path):
    path = tmp_path / "same.db"
    setup = connect(path)
    migrate(setup)
    _obligation_id, incident_id = _ready(setup)
    ref = _grant(setup, incident_id)
    setup.close()
    client = Funded()
    barrier = threading.Barrier(2)
    found: list[str] = []

    def worker() -> None:
        local = connect(path)
        try:
            barrier.wait()
            found.append(execute(local, client, ref.id, as_of=SEEN).remote_id)
        finally:
            local.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check = connect(path)
    assert found == ["awx-new", "awx-new"] and len(client.created) == 1
    assert _count(check, "payout_funding") == 1
    assert check.execute("SELECT COUNT(*) FROM balance_snapshot WHERE label = 'before'").fetchone()[0] == 1
    assert check.execute("SELECT COUNT(*) FROM balance_snapshot WHERE label = 'after'").fetchone()[0] == 1


def _second_obligation(conn) -> int:
    supplier = conn.execute(
        "INSERT INTO supplier (name, contact, phone_on_file) VALUES ('Nord Garn GmbH', 'ap@nord.example', '+49-30-111111')"
    ).lastrowid
    conn.execute(
        """
        INSERT INTO bank_detail_version (supplier_id, beneficiary_id, details_hash, bank_name, account_last4,
            source, verified_by, verified_at)
        VALUES (?, 'ben_verified', 'callback-v2', 'Deutsche Kreditbank', '7782', 'verified_callback',
                'finance-approver', ?)
        """,
        (supplier, SEEN),
    )
    obligation = conn.execute(
        "INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status) "
        "VALUES ('INV-2292', '4200.00', 'EUR', ?, 'OPEN')",
        (supplier,),
    ).lastrowid
    transfer = conn.execute(
        """
        INSERT INTO transfer_record (airwallex_id, request_id, obligation_id, origin, details_hash, amount,
            currency, status, failure_code, failure_message, reference)
        VALUES ('awx-hero2', 'original-payment-2', ?, 'settlewell', 'onboarding-v1', '4200.00', 'EUR',
            'CANCELLED', '90701', '90701: Account closed', 'INV-2292')
        """,
        (obligation,),
    ).lastrowid
    conn.execute("UPDATE obligation SET live_transfer_id = ? WHERE id = ?", (transfer, obligation))
    conn.execute(
        "INSERT INTO status_snapshot (transfer_record_id, status, source, seen_at) VALUES (?, 'CANCELLED', 'poll', ?)",
        (transfer, SEEN),
    )
    incident = conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, NULL, 'OPEN')", (obligation,)
    ).lastrowid
    conn.commit()
    return int(incident)


def test_two_obligations_do_not_spend_the_same_stale_balance():
    conn = connect()
    _obligation_id, first_incident = _ready(conn)
    second_incident = _second_obligation(conn)
    first = _grant(conn, first_incident)
    second = _grant(conn, second_incident)
    # Every read says 6,000.00 available: enough for one payout plus floor, never for two.
    client = Funded(balances=[rows("6000.00")])
    assert execute(conn, client, first.id, as_of=SEEN).remote_id == "awx-new"
    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, second.id, as_of=SEEN)
    assert caught.value.code == "balance"
    assert len(client.created) == 1
    assert conn.execute("SELECT state FROM execution_intent WHERE id = ?", (second.id,)).fetchone()[0] == "proposed"
    hold = conn.execute("SELECT hold_state FROM payout_funding").fetchall()
    assert [h["hold_state"] for h in hold] == ["HELD"]  # a read never releases the hold


def test_a_definitive_first_send_refusal_releases_only_its_own_hold():
    from backend.airwallex.client import AirwallexError

    conn, client, ref = _setup()

    def refuse(**kwargs):
        client.created.append(kwargs)
        raise AirwallexError(400, "insufficient_funds", "no")

    client.create_transfer = refuse
    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)
    assert caught.value.code == "refused"
    row = conn.execute("SELECT state, hold_state FROM payout_funding").fetchone()
    assert (row["state"], row["hold_state"]) == ("REFUSED", "RELEASED")


def test_an_unknown_first_send_keeps_its_hold():
    conn, client, ref = _setup(crash=True)
    with pytest.raises(RuntimeError):
        execute(conn, client, ref.id, as_of=SEEN)
    assert conn.execute("SELECT hold_state FROM payout_funding").fetchone()[0] == "HELD"


def _dump(conn):
    return [
        [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("balance_snapshot", "audit_event", "payout_funding", "execution_intent", "approval")
    ]


def _missing_after(tmp_path):
    conn = connect(tmp_path / "tamper.db")
    migrate(conn)
    _obligation_id, incident_id = _ready(conn)
    ref = _grant(conn, incident_id)
    client = Funded(balances=[rows(), TimeoutError("after read"), rows("5000.00")])
    execute(conn, client, ref.id, as_of=SEEN)
    assert conn.execute("SELECT after_state FROM payout_funding").fetchone()[0] == "MISSING"
    return conn, client, ref


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE approval SET binding_hash = 'forged'",
        "UPDATE approval SET used_at = NULL",
        "UPDATE approval SET incident_id = incident_id + 99",
        "UPDATE payout_funding SET amount = '1.00'",
        "UPDATE payout_funding SET currency = 'USD'",
        "UPDATE payout_funding SET obligation_id = obligation_id + 99",
        "UPDATE payout_funding SET request_id = 'stl-forged-request-1'",
        "UPDATE execution_intent SET amount = '4300.00'",
    ],
)
def test_a_tampered_grant_intent_or_funding_row_blocks_every_resume_write(tmp_path, tamper):
    conn, client, ref = _missing_after(tmp_path)
    conn.execute("PRAGMA foreign_keys = OFF")  # the tamper simulates direct DB edits
    conn.execute(tamper)
    conn.commit()
    before = _dump(conn)
    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)
    assert caught.value.code == "void"
    assert _dump(conn) == before  # no snapshot, audit, or funding write
    assert len(client.created) == 1


def test_an_accepted_send_recovers_its_after_read_after_the_grant_expired(tmp_path):
    conn, client, ref = _missing_after(tmp_path)
    assert execute(conn, client, ref.id, as_of="2026-12-01T00:00:00+00:00").remote_id == "awx-new"
    assert conn.execute("SELECT after_state FROM payout_funding").fetchone()[0] == "OBSERVED"
    assert len(client.created) == 1


def _crash_then(tmp_path):
    conn = connect(tmp_path / "crash.db")
    migrate(conn)
    _obligation_id, incident_id = _ready(conn)
    ref = _grant(conn, incident_id)
    client = Funded(crash=True)
    with pytest.raises(RuntimeError):
        execute(conn, client, ref.id, as_of=SEEN)
    client.crash = False
    return conn, client, ref, incident_id


@pytest.mark.parametrize(
    "change",
    [{"beneficiary_id": "ben_other"}, {"transfer_amount": Decimal("1.00")}, {"reference": "INV-9999"}],
)
def test_a_lookup_that_is_not_our_attempt_binds_and_writes_nothing(tmp_path, change):
    import dataclasses

    conn, client, ref, _incident = _crash_then(tmp_path)
    client.accepted["awx-new"] = dataclasses.replace(client.accepted["awx-new"], **change)
    before = _dump(conn)
    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)
    assert caught.value.code == "in_flight"
    assert conn.execute("SELECT airwallex_id FROM transfer_record WHERE origin = 'settlewell' AND request_id LIKE 'stl-%'").fetchone()[0] is None
    assert conn.execute("SELECT remote_id FROM execution_intent").fetchone()[0] is None
    assert _dump(conn)[:3] == before[:3]  # no snapshot, audit, or funding write
    assert len(client.created) == 1


@pytest.mark.parametrize(
    ("script", "state", "available"),
    [([rows("1.00")], "OBSERVED", ["1.00"]), ([TimeoutError("x")], "MISSING", [])],
)
def test_poll_recovery_of_an_accepted_send_makes_the_after_state_truthful(tmp_path, script, state, available):
    from backend.reconcile import poll_incident

    conn, client, _ref, incident_id = _crash_then(tmp_path)
    client.script = script
    client.balance_calls = 0
    poll_incident(conn, client, incident_id, seen_at=SEEN)
    assert len(client.created) == 1
    assert conn.execute("SELECT remote_id FROM execution_intent").fetchone()[0] == "awx-new"
    row = conn.execute("SELECT state, after_state FROM payout_funding").fetchone()
    assert (row["state"], row["after_state"]) == ("ACCEPTED", state)
    snaps = conn.execute("SELECT available FROM balance_snapshot WHERE label = 'after'").fetchall()
    assert [s["available"] for s in snaps] == available

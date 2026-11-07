"""Durable, complete-outcome reconciliation. On-disk db, new connections. No network."""

from decimal import Decimal

import pytest

from backend.airwallex.types import (
    Balance,
    BalanceHistoryItem,
    BalanceHistoryPage,
    Transfer,
    TransferPage,
)
from backend.approvals import AP_CLERK, FINANCE_APPROVER, approve
from backend.execute import execute, intent_terms, open_intent
from backend.ledger import (
    append_audit,
    append_status_snapshot,
    connect,
    insert_external_transfer,
    seed_hero,
)
from backend.reconcile import poll_incident
from backend.replace_payment import ReplaceError

SEEN = "2026-11-03T12:00:00+00:00"
LATER = "2026-11-03T12:30:00+00:00"
EXPIRES = "2026-11-03T13:00:00+00:00"


def _t(transfer_id, status, request_id, **overrides):
    fields = {
        "id": transfer_id,
        "status": status,
        "request_id": request_id,
        "transfer_amount": Decimal("4200.00"),
        "transfer_currency": "EUR",
        "transfer_method": "SWIFT",
        "fee_amount": Decimal("13.47"),
        "reference": "INV-2291",
        "beneficiary_id": "ben_onboarding",
    }
    fields.update(overrides)
    return Transfer(**fields)


class World:
    """Per-id statuses and per-id reversal sources."""

    def __init__(self, amount="4200.00"):
        self.remote = {
            "awx-hero": _t(
                "awx-hero",
                "CANCELLED",
                "hero-inv2291",
                failure_reason="90802: Beneficiary bank returned",
                transfer_amount=Decimal(amount),
            )
        }
        self.reversals = {"awx-hero": Decimal(amount)}
        self.lookup_result = None
        self.mode = "ok"
        self.fail_get: set[str] = set()
        self.created: list[dict] = []
        self.crash = False
        self.page_size = 50

    def get_transfer(self, transfer_id):
        if transfer_id in self.fail_get:
            raise TimeoutError("get")
        return self.remote[transfer_id]

    def list_transfers(self, **kwargs):
        wanted = kwargs.get("request_id")
        if wanted is not None:
            if self.mode == "error":
                raise TimeoutError("lookup")
            if self.mode == "oserror":
                raise ConnectionResetError("lookup")
            if self.mode == "empty":
                return TransferPage(items=(), page_after=None)
            if self.lookup_result is not None:
                return TransferPage(items=(self.lookup_result,), page_after=None)
            found = tuple(t for t in self.remote.values() if t.request_id == wanted)
            return TransferPage(items=found, page_after=None)
        items = tuple(self.remote.values())
        start = int(kwargs.get("page") or 0)
        chunk = items[start : start + self.page_size]
        more = start + self.page_size < len(items)
        return TransferPage(items=chunk, page_after=str(start + self.page_size) if more else None)

    def balance_history(self, **_kwargs):
        items = tuple(
            BalanceHistoryItem(
                id=f"rev-{source}",
                source=source,
                amount=amount,
                currency="EUR",
                balance=None,
                description=None,
                fee=Decimal(0),
                posted_at=None,
                source_type="PAYOUT",
                transaction_type="PAYOUT_REVERSAL",
                account_type=None,
                reference="INV-2291",
            )
            for source, amount in self.reversals.items()
        )
        return BalanceHistoryPage(items=items, has_more=False, page_after=None)

    def current_balances(self):
        value = Decimal("100000.00")
        return [Balance("EUR", value, Decimal(0), Decimal(0), value, Decimal(0))]

    def create_transfer(self, **kwargs):
        self.created.append(kwargs)
        new_id = "awx-new" if len(self.created) == 1 else f"awx-new{len(self.created)}"
        accepted = _t(
            new_id,
            "SCHEDULED",
            kwargs["request_id"],
            beneficiary_id=kwargs["beneficiary_id"],
            transfer_amount=kwargs["transfer_amount"],
        )
        self.remote[new_id] = accepted
        if self.crash:
            raise RuntimeError("crash")
        return accepted


def _hero(path):
    conn = connect(path)
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    conn.execute(
        "UPDATE transfer_record SET status = 'CANCELLED', failure_code = '90802' WHERE airwallex_id = 'awx-hero'"
    )
    conn.execute("UPDATE bank_detail_version SET beneficiary_id = 'ben_onboarding' WHERE source = 'onboarding'")
    hero = conn.execute("SELECT id FROM transfer_record WHERE airwallex_id = 'awx-hero'").fetchone()
    append_status_snapshot(
        conn,
        transfer_record_id=hero["id"],
        status="CANCELLED",
        failure_code="90802",
        failure_message=None,
        source="poll",
        seen_at=SEEN,
    )
    conn.commit()
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?", (incident_id,)
    ).fetchone()["obligation_id"]
    return conn, incident_id, obligation_id


def _grant(conn, incident_id, obligation_id, amount="4200.00"):
    ref = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="REPLACE_SAME",
        amount=amount,
        currency="EUR",
        beneficiary_id="ben_onboarding",
        details_hash="onboarding-v1",
        proposer=AP_CLERK,
        failure_code="90802",
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(conn, ref.approval_id, intent_terms(conn, ref.id), approver=FINANCE_APPROVER, used_at=SEEN)
    return ref


def _external(conn, obligation_id, transfer_id, status):
    insert_external_transfer(
        conn,
        airwallex_id=transfer_id,
        request_id="colleague-hand",
        obligation_id=obligation_id,
        amount="4200.00",
        currency="EUR",
        status=status,
        reference="INV-2291",
    )
    conn.commit()


def _state(conn, incident_id):
    return (
        conn.execute("SELECT state FROM incident WHERE id = ?", (incident_id,)).fetchone()["state"],
        conn.execute("SELECT status FROM obligation").fetchone()["status"],
    )


def _audits(conn, kind):
    rows = conn.execute("SELECT detail FROM audit_event WHERE kind = ?", (kind,)).fetchall()
    return [row["detail"] for row in rows]


def _crashed(path, amount="4200.00"):
    conn, incident_id, obligation_id = _hero(path)
    ref = _grant(conn, incident_id, obligation_id, amount)
    world = World(amount)
    world.crash = True
    with pytest.raises(RuntimeError, match="crash"):
        execute(conn, world, ref.id, as_of=SEEN)
    conn.close()
    world.crash = False
    return world, incident_id, ref


def test_poll_recovers_a_lost_accepted_send_by_request_id_with_no_new_create(tmp_path):
    path = tmp_path / "a.db"
    world, incident_id, ref = _crashed(path)
    conn = connect(path)

    poll_incident(conn, world, incident_id, seen_at=LATER)

    assert len(world.created) == 1
    row = conn.execute("SELECT * FROM transfer_record WHERE request_id = 'stl-inv2291-1'").fetchone()
    assert (row["airwallex_id"], row["origin"], row["amount"], row["details_hash"]) == (
        "awx-new",
        "settlewell",
        "4200.00",
        "onboarding-v1",
    )
    intent = conn.execute("SELECT remote_id FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
    assert intent["remote_id"] == "awx-new"
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    conn.close()


def _second_incident(conn, obligation_id):
    cur = conn.execute("INSERT INTO incident (obligation_id, state) VALUES (?, 'OPEN')", (obligation_id,))
    conn.commit()
    return cur.lastrowid


def test_poll_of_a_second_incident_recovers_the_original_owner_attempt(tmp_path):
    path = tmp_path / "own.db"
    world, incident_id, ref = _crashed(path)
    conn = connect(path)
    obligation_id = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (incident_id,)).fetchone()[0]
    second = _second_incident(conn, obligation_id)

    poll_incident(conn, world, second, seen_at=LATER)

    assert len(world.created) == 1
    row = conn.execute("SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'").fetchone()
    assert row["airwallex_id"] == "awx-new"
    intent = conn.execute("SELECT incident_id, remote_id FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
    assert (intent["incident_id"], intent["remote_id"]) == (incident_id, "awx-new")
    conn.close()


def test_owner_approval_incident_mismatch_stays_unresolved(tmp_path):
    path = tmp_path / "own2.db"
    world, incident_id, ref = _crashed(path)
    conn = connect(path)
    obligation_id = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (incident_id,)).fetchone()[0]
    second = _second_incident(conn, obligation_id)
    conn.execute("UPDATE approval SET incident_id = ? WHERE id = ?", (second, ref.approval_id))
    conn.commit()

    poll_incident(conn, world, incident_id, seen_at=LATER)
    poll_incident(conn, world, second, seen_at=LATER)

    row = conn.execute("SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'").fetchone()
    assert row["airwallex_id"] is None
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert len(world.created) == 1
    conn.close()


@pytest.mark.parametrize("mode", ["error", "empty", "oserror"])
def test_failed_or_empty_request_lookup_keeps_the_attempt_in_flight(tmp_path, mode):
    path = tmp_path / "b.db"
    world, incident_id, _ref = _crashed(path)
    world.mode = mode
    conn = connect(path)

    poll_incident(conn, world, incident_id, seen_at=LATER)

    rows = conn.execute(
        "SELECT airwallex_id, status FROM transfer_record WHERE request_id LIKE 'stl-%'"
    ).fetchall()
    assert [(r["airwallex_id"], r["status"]) for r in rows] == [(None, "IN_FLIGHT")]
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert _audits(conn, "attempt_unresolved") == ["stl-inv2291-1"]
    assert len(world.created) == 1
    world.mode = "ok"
    poll_incident(conn, world, incident_id, seen_at=LATER)
    bound = conn.execute("SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'")
    assert bound.fetchone()["airwallex_id"] == "awx-new"
    conn.close()


@pytest.mark.parametrize(
    "override",
    [
        {"beneficiary_id": "ben_other"},
        {"request_id": None},
        {"request_id": "stl-other-9"},
        {"transfer_amount": Decimal("4200.01")},
    ],
)
def test_recovery_refuses_a_remote_that_does_not_match_the_approved_terms(tmp_path, override):
    path = tmp_path / "c.db"
    world, incident_id, _ref = _crashed(path)
    fields = {"request_id": "stl-inv2291-1"}
    fields.update(override)
    world.lookup_result = _t("awx-new", "SCHEDULED", **fields)
    conn = connect(path)

    poll_incident(conn, world, incident_id, seen_at=LATER)

    row = conn.execute("SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'").fetchone()
    assert row["airwallex_id"] is None
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert _audits(conn, "recovery_mismatch") == ["stl-inv2291-1"]
    conn.close()


def test_tracked_external_paid_then_later_failure_reopens_and_a_new_grant_replaces(tmp_path):
    path = tmp_path / "d.db"
    conn, incident_id, obligation_id = _hero(path)
    _external(conn, obligation_id, "awx-col", "SENT")
    world = World()
    world.remote["awx-col"] = _t("awx-col", "PROCESSING", "colleague-hand")
    conn.close()

    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id)[0] == "OPEN"
    stored = conn.execute("SELECT status FROM transfer_record WHERE airwallex_id = 'awx-col'").fetchone()
    assert stored["status"] == "PROCESSING"
    conn.close()

    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    assert len(_audits(conn, "incident_closed")) == 1
    assert _audits(conn, "original_disposition") == ["awx-hero:returned"]
    assert "awx-col:PAID" in _audits(conn, "replacement_disposition")
    conn.execute(  # a colleague close moved the live pointer to the colleague payment
        "UPDATE obligation SET live_transfer_id = (SELECT id FROM transfer_record WHERE airwallex_id = 'awx-col')"
    )
    conn.commit()
    conn.close()

    world.remote["awx-col"] = _t("awx-col", "CANCELLED", "colleague-hand")
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert len(_audits(conn, "incident_reopened")) == 1
    pointer = conn.execute(
        "SELECT airwallex_id FROM obligation JOIN transfer_record ON transfer_record.id = live_transfer_id"
    ).fetchone()
    assert pointer["airwallex_id"] == "awx-hero"

    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")  # stale
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    stored = conn.execute("SELECT status FROM transfer_record WHERE airwallex_id = 'awx-col'").fetchone()
    assert stored["status"] == "CANCELLED"

    world.remote["awx-col"] = _t("awx-col", "CANCELLED", "colleague-hand")
    ref = _grant(conn, incident_id, obligation_id)
    result = execute(conn, world, ref.id, as_of=SEEN)
    assert result.remote_id == "awx-new"
    assert len(world.created) == 1
    conn.close()


def test_original_returned_after_replacement_paid_closes_once_the_whole_set_is_known(tmp_path):
    path = tmp_path / "e.db"
    conn, incident_id, obligation_id = _hero(path)
    conn.execute("UPDATE transfer_record SET status = 'SENT' WHERE airwallex_id = 'awx-hero'")
    _external(conn, obligation_id, "awx-col", "SENT")
    world = World()
    world.remote["awx-hero"] = _t("awx-hero", "SENT", "hero-inv2291")
    world.reversals = {}
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")
    conn.close()

    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id)[0] == "OPEN"
    assert _state(conn, incident_id)[1] != "PAID_ONCE"

    world.remote["awx-hero"] = _t("awx-hero", "CANCELLED", "hero-inv2291")  # no reversal yet
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id)[0] == "OPEN"
    assert _state(conn, incident_id)[1] != "PAID_ONCE"

    world.reversals = {"awx-hero": Decimal("4200.00")}
    conn.close()
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    conn.close()


def test_original_paid_then_reversed_reopens_and_a_stale_paid_does_not_reclose(tmp_path):
    path = tmp_path / "f.db"
    conn, incident_id, _obligation_id = _hero(path)
    conn.execute("UPDATE transfer_record SET status = 'SENT' WHERE airwallex_id = 'awx-hero'")
    conn.commit()
    world = World()
    world.remote["awx-hero"] = _t("awx-hero", "PAID", "hero-inv2291")
    world.reversals = {}
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    conn.close()

    world.remote["awx-hero"] = _t("awx-hero", "CANCELLED", "hero-inv2291")
    world.reversals = {"awx-hero": Decimal("4200.00")}
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")

    world.remote["awx-hero"] = _t("awx-hero", "PAID", "hero-inv2291")
    world.reversals = {}  # the reversal fell out of the history window
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    conn.close()


def test_unknown_supplier_failed_lookup_or_two_paid_never_close(tmp_path):
    path = tmp_path / "g.db"
    conn, incident_id, obligation_id = _hero(path)
    _external(conn, obligation_id, "awx-col", "PAID")
    world = World()
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")
    world.page_size = 1
    world.remote["awx-stranger"] = _t("awx-stranger", "PAID", "other", beneficiary_id="ben_unknown")

    poll_incident(conn, world, incident_id, seen_at=LATER)  # stranger is on a later page
    assert _state(conn, incident_id)[0] == "OPEN"
    stranger = conn.execute("SELECT origin FROM transfer_record WHERE airwallex_id = 'awx-stranger'").fetchone()
    assert stranger["origin"] == "external"
    world.remote["awx-stranger"] = _t("awx-stranger", "CANCELLED", "other", beneficiary_id="ben_unknown")

    world.fail_get = {"awx-col"}
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id)[0] == "OPEN"
    assert _audits(conn, "outcome_lookup_failed")
    conn.close()


def test_two_paid_payments_never_close(tmp_path):
    path = tmp_path / "h.db"
    conn, incident_id, obligation_id = _hero(path)
    conn.execute("UPDATE transfer_record SET status = 'SENT' WHERE airwallex_id = 'awx-hero'")
    _external(conn, obligation_id, "awx-col", "PAID")
    world = World()
    world.remote["awx-hero"] = _t("awx-hero", "PAID", "hero-inv2291")
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")
    world.reversals = {}

    poll_incident(conn, world, incident_id, seen_at=LATER)

    assert _state(conn, incident_id)[0] == "OPEN"
    assert _state(conn, incident_id)[1] != "PAID_ONCE"
    assert _audits(conn, "outcome_conflict")
    conn.close()


def test_stale_live_status_never_overwrites_a_terminal_failure_across_restarts(tmp_path):
    path = tmp_path / "i.db"
    conn, incident_id, obligation_id = _hero(path)
    _external(conn, obligation_id, "awx-col", "SENT")
    conn.close()
    world = World()
    for status in ("FAILED", "SENT", "PROCESSING", "PAID"):
        world.remote["awx-col"] = _t("awx-col", status, "colleague-hand")
        conn = connect(path)
        poll_incident(conn, world, incident_id, seen_at=LATER)
        stored = conn.execute("SELECT status FROM transfer_record WHERE airwallex_id = 'awx-col'").fetchone()
        assert stored["status"] == "FAILED"
        assert _state(conn, incident_id)[0] == "OPEN"
        assert _state(conn, incident_id)[1] != "PAID_ONCE"
        conn.close()
    conn = connect(path)
    assert _audits(conn, "incident_closed") == []
    assert "awx-col:SENT" in _audits(conn, "stale_status_ignored")
    conn.close()


def _closed_by_colleague(path):
    conn, incident_id, obligation_id = _hero(path)
    _external(conn, obligation_id, "awx-col", "SENT")
    world = World()
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    conn.close()
    return connect(path), incident_id, obligation_id, world


def test_a_new_live_unknown_supplier_extra_reopens_a_closed_incident(tmp_path):
    conn, incident_id, _obligation_id, world = _closed_by_colleague(tmp_path / "j.db")
    world.remote["awx-x"] = _t("awx-x", "SCHEDULED", "other", beneficiary_id="ben_unknown")

    poll_incident(conn, world, incident_id, seen_at=LATER)

    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert len(_audits(conn, "incident_reopened")) == 1
    conn.close()


def test_a_recorded_unresolved_attempt_reopens_a_closed_incident(tmp_path):
    conn, incident_id, obligation_id, world = _closed_by_colleague(tmp_path / "k.db")
    conn.execute(
        """
        INSERT INTO transfer_record (request_id, obligation_id, origin, details_hash, amount, currency,
                                     status, reference)
        VALUES ('stl-inv2291-1', ?, 'settlewell', 'onboarding-v1', '4200.00', 'EUR', 'IN_FLIGHT', 'INV-2291')
        """,
        (obligation_id,),
    )
    conn.commit()

    poll_incident(conn, world, incident_id, seen_at=LATER)

    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert _audits(conn, "attempt_unresolved") == ["stl-inv2291-1"]
    conn.close()


def test_a_pure_read_failure_of_accounted_facts_holds_the_prior_closure(tmp_path):
    conn, incident_id, _obligation_id, world = _closed_by_colleague(tmp_path / "l.db")
    world.fail_get = {"awx-col"}

    poll_incident(conn, world, incident_id, seen_at=LATER)

    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    assert _audits(conn, "outcome_lookup_failed") == ["awx-col"]
    conn.close()


def _reverse_bundle(monkeypatch):
    from backend import reconcile

    real = reconcile.hero_bundle

    def reversed_bundle(conn, incident_id):
        bundle = dict(real(conn, incident_id))
        bundle["transfers"] = list(reversed(bundle["transfers"]))
        return bundle

    monkeypatch.setattr(reconcile, "hero_bundle", reversed_bundle)


def test_original_identity_is_independent_of_row_order_and_pointer_is_restored(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    world, incident_id, _ref = _crashed(path)
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)  # binds awx-new, SCHEDULED
    _reverse_bundle(monkeypatch)

    world.remote["awx-new"] = _t("awx-new", "PAID", "stl-inv2291-1")
    poll_incident(conn, world, incident_id, seen_at=LATER)

    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    assert _audits(conn, "original_disposition") == ["awx-hero:returned"]
    assert "awx-new:PAID" in _audits(conn, "replacement_disposition")
    assert not [d for d in _audits(conn, "replacement_disposition") if d.startswith("awx-hero")]

    conn.execute(  # the live pointer had moved to the owned replacement
        "UPDATE obligation SET live_transfer_id = (SELECT id FROM transfer_record WHERE airwallex_id = 'awx-new')"
    )
    conn.commit()
    world.remote["awx-new"] = _t("awx-new", "CANCELLED", "stl-inv2291-1")
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    pointer = conn.execute(
        "SELECT airwallex_id FROM obligation JOIN transfer_record ON transfer_record.id = live_transfer_id"
    ).fetchone()
    assert pointer["airwallex_id"] == "awx-hero"
    conn.close()


def test_ambiguous_original_role_holds_instead_of_guessing(tmp_path):
    path = tmp_path / "n.db"
    world, incident_id, _ref = _crashed(path)
    conn = connect(path)
    conn.execute(  # pointer names our own replacement and no disposition was ever recorded
        "UPDATE obligation SET live_transfer_id = (SELECT id FROM transfer_record WHERE request_id = 'stl-inv2291-1')"
    )
    conn.execute(  # ...and two unowned settlewell rows could each be the original
        """
        INSERT INTO transfer_record (airwallex_id, request_id, obligation_id, origin, details_hash, amount,
                                     currency, status, reference)
        SELECT 'awx-old', 'hero-older', obligation_id, 'settlewell', details_hash, amount, currency,
               'CANCELLED', reference
        FROM transfer_record WHERE airwallex_id = 'awx-hero'
        """
    )
    conn.commit()
    world.remote["awx-old"] = _t("awx-old", "CANCELLED", "hero-older")
    world.remote["awx-new"] = _t("awx-new", "PAID", "stl-inv2291-1")

    poll_incident(conn, world, incident_id, seen_at=LATER)

    assert _state(conn, incident_id)[0] == "OPEN"
    assert _state(conn, incident_id)[1] != "PAID_ONCE"
    assert _audits(conn, "original_ambiguous")
    assert _audits(conn, "original_disposition") == []
    conn.close()


def test_recovery_validates_against_the_approved_amount_not_the_stale_local_one(tmp_path):
    path = tmp_path / "o.db"
    world, incident_id, _ref = _crashed(path, "4200.50")
    conn = connect(path)
    assert conn.execute("SELECT amount FROM obligation").fetchone()["amount"] == "4200.00"

    poll_incident(conn, world, incident_id, seen_at=LATER)

    row = conn.execute("SELECT airwallex_id, amount FROM transfer_record WHERE request_id = 'stl-inv2291-1'").fetchone()
    assert (row["airwallex_id"], row["amount"]) == ("awx-new", "4200.50")
    assert len(world.created) == 1
    conn.close()


def _pointer(conn):
    row = conn.execute(
        "SELECT airwallex_id FROM obligation JOIN transfer_record ON transfer_record.id = live_transfer_id"
    ).fetchone()
    return row["airwallex_id"]


def _point_at(conn, airwallex_id):
    conn.execute(
        "UPDATE obligation SET live_transfer_id = (SELECT id FROM transfer_record WHERE airwallex_id = ?)",
        (airwallex_id,),
    )


@pytest.mark.parametrize("kinds", [("colleague_repaid",), ("colleague_tracked", "colleague_repaid")])
def test_legacy_colleague_closure_pointer_is_never_the_original_and_a_new_grant_replaces(tmp_path, kinds):
    """Old 9643 state: closed by a colleague, pointer on the colleague, no original_disposition yet."""
    path = tmp_path / "p.db"
    conn, incident_id, obligation_id = _hero(path)
    _external(conn, obligation_id, "awx-col", "PAID")
    for kind in kinds:
        append_audit(conn, incident_id=incident_id, kind=kind, detail="awx-col", created_at=SEEN)
    _point_at(conn, "awx-col")
    conn.execute("UPDATE obligation SET status = 'PAID_ONCE'")
    conn.execute("UPDATE incident SET state = 'CLOSED' WHERE id = ?", (incident_id,))
    conn.commit()
    conn.close()
    world = World()
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")

    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    assert _audits(conn, "original_disposition") == ["awx-hero:returned"]
    assert "awx-col:PAID" in _audits(conn, "replacement_disposition")
    conn.close()

    world.remote["awx-col"] = _t("awx-col", "CANCELLED", "colleague-hand")
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert _audits(conn, "original_disposition") == ["awx-hero:returned"]
    assert _pointer(conn) == "awx-hero"
    conn.close()

    conn = connect(path)
    ref = _grant(conn, incident_id, obligation_id)
    result = execute(conn, world, ref.id, as_of=SEEN)
    assert result.remote_id == "awx-new"
    assert len(world.created) == 1
    conn.close()


def test_pointer_on_our_own_replacement_falls_back_to_the_one_unowned_original(tmp_path):
    path = tmp_path / "q.db"
    world, incident_id, _ref = _crashed(path)
    conn = connect(path)
    conn.execute(  # pointer moved to our own replacement attempt; no disposition was ever recorded
        "UPDATE obligation SET live_transfer_id = (SELECT id FROM transfer_record WHERE request_id = 'stl-inv2291-1')"
    )
    conn.commit()
    conn.close()

    world.remote["awx-new"] = _t("awx-new", "PAID", "stl-inv2291-1")
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    assert _audits(conn, "original_disposition") == ["awx-hero:returned"]
    assert _audits(conn, "original_ambiguous") == []
    conn.close()

    world.remote["awx-new"] = _t("awx-new", "CANCELLED", "stl-inv2291-1")
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert _pointer(conn) == "awx-hero"
    conn.close()

    conn = connect(path)
    obligation_id = conn.execute("SELECT id FROM obligation").fetchone()["id"]
    ref = _grant(conn, incident_id, obligation_id)
    with pytest.raises(ReplaceError, match="refused_sequence"):  # one accepted replacement per attempt chain
        execute(conn, world, ref.id, as_of=SEEN)
    assert len(world.created) == 1
    conn.close()


def test_pointer_on_our_refused_attempt_falls_back_and_a_new_grant_replaces_after_colleague_failure(tmp_path):
    path = tmp_path / "t.db"
    conn, incident_id, obligation_id = _hero(path)
    conn.execute(
        """
        INSERT INTO transfer_record (request_id, obligation_id, origin, details_hash, amount, currency,
                                     status, reference)
        VALUES ('stl-inv2291-1', ?, 'settlewell', 'onboarding-v1', '4200.00', 'EUR', 'REFUSED', 'INV-2291')
        """,
        (obligation_id,),
    )
    conn.execute(  # pointer on our own refused attempt, no disposition recorded
        "UPDATE obligation SET live_transfer_id = (SELECT id FROM transfer_record WHERE request_id = 'stl-inv2291-1')"
    )
    _external(conn, obligation_id, "awx-col", "SENT")
    append_audit(conn, incident_id=incident_id, kind="colleague_tracked", detail="awx-col", created_at=SEEN)
    conn.commit()
    conn.close()
    world = World()
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")

    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    assert _audits(conn, "original_disposition") == ["awx-hero:returned"]
    assert _audits(conn, "original_ambiguous") == []
    conn.close()

    world.remote["awx-col"] = _t("awx-col", "CANCELLED", "colleague-hand")
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert _pointer(conn) == "awx-hero"
    conn.close()

    conn = connect(path)
    ref = _grant(conn, incident_id, obligation_id)
    result = execute(conn, world, ref.id, as_of=SEEN)
    assert result.remote_id == "awx-new"
    assert world.created[0]["request_id"] == "stl-inv2291-2"
    assert len(world.created) == 1
    conn.close()


def test_an_unknown_own_attempt_blocks_a_colleague_close_after_restart(tmp_path):
    """execute timed out after the send; the request-id lookup and the listing both miss it."""
    from backend.repaid import colleague_repaid

    path = tmp_path / "u.db"
    world, incident_id, ref = _crashed(path)
    world.remote.pop("awx-new")  # accepted outcome unknown: absent from discovery
    world.mode = "empty"
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")
    conn = connect(path)
    obligation_id = conn.execute("SELECT id FROM obligation").fetchone()["id"]
    pending = conn.execute("SELECT airwallex_id, status FROM transfer_record WHERE request_id = 'stl-inv2291-1'")
    assert tuple(pending.fetchone()) == (None, "IN_FLIGHT")

    decision = colleague_repaid(conn, world, obligation_id=obligation_id, incident_ref=str(incident_id))

    assert decision.action == "hold"
    assert _state(conn, incident_id)[0] == "OPEN"
    assert _state(conn, incident_id)[1] != "PAID_ONCE"
    assert _audits(conn, "colleague_repaid") == []
    assert _audits(conn, "attempt_unresolved") == ["stl-inv2291-1"]
    conn.close()

    conn = connect(path)  # retrying the same intent recovers by request id; it never closes or re-sends
    with pytest.raises(ReplaceError):
        execute(conn, world, ref.id, as_of=SEEN)
    assert _state(conn, incident_id)[0] == "OPEN"
    assert _audits(conn, "colleague_repaid") == []
    assert len(world.created) == 1
    conn.close()


def test_an_external_origin_original_under_the_pointer_is_supported(tmp_path):
    path = tmp_path / "r.db"
    conn, incident_id, obligation_id = _hero(path)
    conn.execute("UPDATE transfer_record SET origin = 'external' WHERE airwallex_id = 'awx-hero'")
    _external(conn, obligation_id, "awx-col", "SENT")
    append_audit(conn, incident_id=incident_id, kind="colleague_tracked", detail="awx-col", created_at=SEEN)
    conn.commit()
    assert _pointer(conn) == "awx-hero"
    conn.close()
    world = World()
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")

    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("CLOSED", "PAID_ONCE")
    assert _audits(conn, "original_disposition") == ["awx-hero:returned"]
    _point_at(conn, "awx-col")  # a later colleague close moved the pointer
    conn.commit()
    conn.close()

    world.remote["awx-col"] = _t("awx-col", "CANCELLED", "colleague-hand")
    conn = connect(path)
    poll_incident(conn, world, incident_id, seen_at=LATER)
    assert _state(conn, incident_id) == ("OPEN", "IN_FLIGHT")
    assert _pointer(conn) == "awx-hero"
    conn.close()


def test_external_original_with_pointer_on_a_colleague_and_no_disposition_holds(tmp_path):
    path = tmp_path / "s.db"
    conn, incident_id, obligation_id = _hero(path)
    conn.execute("UPDATE transfer_record SET origin = 'external' WHERE airwallex_id = 'awx-hero'")
    _external(conn, obligation_id, "awx-col", "PAID")
    append_audit(conn, incident_id=incident_id, kind="colleague_repaid", detail="awx-col", created_at=SEEN)
    _point_at(conn, "awx-col")
    conn.commit()
    world = World()
    world.remote["awx-col"] = _t("awx-col", "PAID", "colleague-hand")

    poll_incident(conn, world, incident_id, seen_at=LATER)

    assert _state(conn, incident_id)[0] == "OPEN"
    assert _audits(conn, "original_ambiguous")
    assert _audits(conn, "original_disposition") == []
    conn.close()

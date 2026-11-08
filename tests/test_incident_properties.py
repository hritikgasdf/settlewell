"""Generative money-invariant tests (P1-7). Real execute(), approval ledger, reconcile and a
stateful typed remote. Offline, no sleeps, bounded. Failures replay from Hypothesis' printed seed.

Nothing here guards execute() behind an "approved" check: every unapproved, tampered, expired,
self-approved or replayed attempt is sent to the real execute() and must be rejected by it.
"""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from backend.approvals import AP_CLERK
from backend.execute import ExecuteError, approve_intent, execute
from backend.incident_world import AMOUNT, ORIGINAL, SEEN, World, make_world
from backend.ledger import connect
from backend.replace_payment import ReplaceError

LATE = "2026-11-03T14:00:00+00:00"  # after the intent's 13:00 expiry
LIVE = frozenset({"SCHEDULED", "PROCESSING", "SENT", "PAID", "OVERDUE"})
INVOICE = "INV-2291"
BOUNDED = settings(
    max_examples=50, stateful_step_count=10, deadline=None, derandomize=False,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

cents = st.one_of(
    st.sampled_from([Decimal(v) for v in ("8000", "50000", "4300", "9000", "50000", "0", "4213.46", "4214")]),
    st.integers(min_value=420_000, max_value=900_000).map(lambda c: Decimal(c) / 100),
)


def _matching(w: World) -> list:
    return [t for t in w.remote.transfers.values() if t.reference == INVOICE]


def _live(w: World) -> int:
    return sum(1 for t in _matching(w) if t.status in LIVE)


def _attempt(w: World, intent_id: int, as_of: str):
    """Call the real execute(). Returns (result, error_code); only known money errors count as a refusal."""
    try:
        return execute(w.conn, w.remote, intent_id, as_of=as_of), None
    except (ExecuteError, ReplaceError) as error:
        return None, error.code
    except TimeoutError:
        return None, "lost_response"


class IncidentMachine(RuleBasedStateMachine):
    """Operations in any order: approvals, replays, tampering, remote statuses, colleagues, restarts."""

    @initialize(
        floor=st.sampled_from([Decimal(500), Decimal(1000), Decimal(2000)]),
        extra=cents,
        fee=st.sampled_from(["13.47", "0.01", "25.00"]),
        status=st.sampled_from(["SENT", "SCHEDULED", "FAILED"]),
        returned=st.sampled_from([True, True, True, False]),
    )
    def start(self, floor, extra, fee, status, returned):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"SETTLEWELL_RESERVE_FLOOR": str(floor)})
        self._env.start()
        self.floor = floor
        self.w = make_world(str(Path(self._tmp.name) / "p.db"), status=status, fee=fee)
        if returned:  # the bank already returned the original, so a replacement is policy-eligible
            self.w.remote.bounce(ORIGINAL, "90802: Bank rejected")
            self.w.poll()
        self.w.remote.balances["EUR"] = floor + extra  # balance >= floor at the start
        self.intents: list[dict] = []
        self.colleagues = 0
        self.closed_seen = False

    def teardown(self):
        self._env.stop()
        self.w.conn.close()
        self._tmp.cleanup()

    # ---- intents and approval ----
    @rule(mode=st.sampled_from(["valid", "valid", "valid", "unapproved", "self", "stranger"]))
    def propose(self, mode):
        w = self.w
        ref = w.propose("REPLACE_SAME", "90802", "ben_onboard", "onboarding-v1", approver=None)
        item = {"id": ref.id, "valid": False, "tampered": False}
        if mode != "unapproved":
            who = {"valid": "finance-approver", "self": AP_CLERK, "stranger": "mallory"}[mode]
            try:
                approve_intent(w.conn, ref.id, approver=who, as_of=SEEN)
                granted = True
            except ExecuteError:
                granted = False
            assert granted == (mode == "valid"), f"{mode} approval outcome wrong"
            item["valid"] = granted
        self.intents.append(item)

    @precondition(lambda self: self.intents)
    @rule(data=st.data())
    def tamper(self, data):
        item = data.draw(st.sampled_from(self.intents))
        if self.w.intent_state(item["id"]) != "proposed":
            return
        self.w.conn.execute("UPDATE execution_intent SET amount = '4201.00' WHERE id = ?", (item["id"],))
        self.w.conn.commit()
        item["tampered"] = True

    @precondition(lambda self: self.intents)
    @rule(data=st.data(), late=st.booleans())
    def execute_any(self, data, late):
        """Real execute() on any intent, valid or not, fresh or replayed. Asserts what it did to money."""
        self._execute(data.draw(st.sampled_from(self.intents)), LATE if late else SEEN)

    @rule()
    def approve_and_pay(self):
        """Steer toward the money path: a fresh, validly approved intent executed at once."""
        self.propose("valid")
        self._execute(self.intents[-1], SEEN)

    def _execute(self, item, as_of):
        w = self.w
        state = w.intent_state(item["id"])
        posts, live, eur = len(w.remote.posts), _live(w), w.remote.eur()
        fee = w.remote.fee
        result, error = _attempt(w, item["id"], as_of)
        grew = len(w.remote.posts) - posts
        if state == "executed":  # replay: no second payment
            assert grew == 0, "replay of a spent intent posted again"
            return
        if not item["valid"] or item["tampered"] or as_of == LATE:
            assert result is None and error is not None and grew == 0, (
                f"invalid attempt (valid={item['valid']}, tampered={item['tampered']}, late={as_of == LATE}) -> {result}/{error}"
            )
            assert w.intent_state(item["id"]) != "executed"
            return
        # valid, fresh, proposed
        if live >= 1:  # property 1: a known live payment means we never add one
            assert grew == 0, "added a payment while another live one existed"
        if eur < AMOUNT + fee + self.floor:  # property 2: low balance fails closed
            assert grew == 0 and result is None, "paid below the reserve floor"
        if grew:
            assert grew == 1
            assert result is not None or error == "lost_response"
            assert eur - w.remote.eur() == AMOUNT + fee, "debit is not principal + fee"
            assert w.remote.eur() >= self.floor, "balance fell below the reserve floor"

    # ---- remote world ----
    @rule()
    def bank_returns_original(self):
        w = self.w
        if w.remote.get_transfer(ORIGINAL).status != "CANCELLED":
            w.remote.bounce(ORIGINAL, "90802: Bank rejected")

    @rule()
    def original_fails_pending_refund(self):
        w = self.w
        if w.remote.get_transfer(ORIGINAL).status in ("SENT", "SCHEDULED"):
            w.remote.set_status(ORIGINAL, "FAILED", failure_reason="90802: Bank rejected")

    @rule(data=st.data(), status=st.sampled_from(["PAID", "FAILED", "PROCESSING", "RETURN"]))
    def our_replacement_moves(self, data, status):
        mine = [t.id for t in self.w.remote.transfers.values() if t.id.startswith("awx-new-")]
        if not mine:
            return
        tid = data.draw(st.sampled_from(mine))
        if status == "RETURN":  # late bank return after a PAID
            if self.w.remote.get_transfer(tid).status == "PAID":
                self.w.remote.bounce(tid, "90802: Bank rejected")
        elif self.w.remote.get_transfer(tid).status != "CANCELLED":
            self.w.remote.set_status(tid, status, failure_reason="90802: Bank rejected" if status == "FAILED" else None)

    @rule(status=st.sampled_from(["PAID", "SCHEDULED", "PROCESSING"]), tagged=st.booleans())
    def colleague_pays(self, status, tagged):
        """An untagged colleague transfer (no request id of ours) for the same invoice."""
        self.colleagues += 1
        self.w.remote.add_transfer(
            f"awx-col-{self.colleagues}", status=status, amount=str(AMOUNT), reference=INVOICE,
            beneficiary_id="ben_onboard" if tagged else None, request_id=f"colleague-{self.colleagues}",
        )

    @rule()
    def response_lost_next(self):
        self.w.remote.accept_then_timeout = True

    @rule()
    def restart(self):
        """New process: reopen the durable ledger. Spent approvals must stay spent."""
        self.w.conn.close()
        self.w.conn = connect(self.w.path)
        for item in self.intents:
            if self.w.intent_state(item["id"]) != "proposed":
                with pytest.raises(ExecuteError):
                    approve_intent(self.w.conn, item["id"], approver="finance-approver", as_of=SEEN)

    @rule()
    def settle_ours_and_poll(self):
        """Steer toward closing: our scheduled payments reach PAID, then the shared reconciler reads."""
        for t in list(self.w.remote.transfers.values()):
            if t.id.startswith("awx-new-") and t.status in ("SCHEDULED", "PROCESSING"):
                self.w.remote.set_status(t.id, "PAID")
        self.poll()

    @rule()
    def poll(self):
        self.w.poll()
        self.check_close()

    # ---- invariants ----
    @invariant()
    def no_double_pay_by_us(self):
        w = self.w
        ids = [p["request_id"] for p in w.remote.posts]
        assert len(ids) == len(set(ids)) and w.remote.duplicate_posts == 0, "same request id posted twice"
        # property 3: never more sends than distinct valid, untampered intents
        spendable = sum(1 for i in self.intents if i["valid"] and not i["tampered"])
        assert len(w.remote.posts) <= spendable, "a remote send without a bound approval"
        paid = sum(1 for t in w.remote.transfers.values() if t.id.startswith("awx-new-"))
        assert paid == len(w.remote.posts)

    @invariant()
    def spent_grants_equal_sends(self):
        used = self.w.conn.execute("SELECT COUNT(*) AS n FROM approval WHERE used_at IS NOT NULL").fetchone()["n"]
        assert len(self.w.remote.posts) <= used

    def check_close(self):
        _assert_close_accounted(self.w)


def _is_closed(w: World) -> bool:
    row = w.conn.execute("SELECT state FROM incident WHERE id = ?", (w.incident_id,)).fetchone()
    status = w.conn.execute("SELECT status FROM obligation WHERE id = ?", (w.obligation_id,)).fetchone()["status"]
    return row["state"] == "CLOSED" or status == "PAID_ONCE"


def _assert_close_accounted(w: World) -> None:
    """Property 4, after a reconcile read: closed only when the full remote set is accounted for."""
    if not _is_closed(w):
        return
    by_status = [t.status for t in _matching(w)]
    assert by_status.count("PAID") == 1, f"closed with {by_status}"
    assert all(s in ("PAID", "CANCELLED") for s in by_status), f"closed with unsettled outcome {by_status}"
    details = " ".join(
        r["detail"] for r in w.conn.execute("SELECT detail FROM audit_event WHERE incident_id = ?", (w.incident_id,))
    )
    for t in _matching(w):
        if t.id.startswith("awx-new-") or t.id == ORIGINAL:
            assert t.id in details, f"no explicit disposition recorded for {t.id}"


IncidentMachine.TestCase.settings = BOUNDED
TestIncidentMachine = IncidentMachine.TestCase


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(events=st.lists(
    st.sampled_from(["replacement_returned", "colleague_paid", "colleague_scheduled", "replacement_failed",
                     "replacement_processing", "poll"]),
    min_size=1, max_size=6,
))
def test_after_a_real_close_every_late_event_is_reconciled(events):
    """Real approved replacement -> PAID -> CLOSED, then a generated order of late remote events.
    After every poll the incident may stay closed only while the complete remote set still shows one PAID."""
    with mock.patch.dict(os.environ, {"SETTLEWELL_RESERVE_FLOOR": "1000"}):
        w = make_world(status="SENT")
        w.remote.bounce(ORIGINAL, "90802: Bank rejected")
        w.remote.balances["EUR"] = Decimal(50000)
        w.poll()
        ref = w.propose("REPLACE_SAME", "90802", "ben_onboard", "onboarding-v1")
        assert w.run(ref.id)[1] is None
        w.remote.set_status("awx-new-1", "PAID")
        w.poll()
        assert _is_closed(w), "positive control: a settled single replacement closes"
        _assert_close_accounted(w)
        for n, event in enumerate(events):
            if event == "replacement_returned" and w.remote.get_transfer("awx-new-1").status == "PAID":
                w.remote.bounce("awx-new-1", "90802: Bank rejected")
            elif event == "replacement_failed":
                w.remote.set_status("awx-new-1", "FAILED", failure_reason="90802: Bank rejected")
            elif event == "replacement_processing":
                w.remote.set_status("awx-new-1", "PROCESSING")
            elif event in ("colleague_paid", "colleague_scheduled"):
                w.remote.add_transfer(f"awx-col-{n}", status="PAID" if event == "colleague_paid" else "SCHEDULED",
                                      amount=str(AMOUNT), reference=INVOICE, beneficiary_id=None,
                                      request_id=f"colleague-{n}")
            w.poll()
            _assert_close_accounted(w)
        assert len(w.remote.posts) == 1, "late events never make us send another payment"


# ---- directed positive controls and fail-closed cases (real paths, no fake guards) ----
def _ready_world(eur: str, floor: str, monkeypatch) -> tuple[World, int]:
    monkeypatch.setenv("SETTLEWELL_RESERVE_FLOOR", floor)
    w = make_world(status="SENT")
    w.remote.bounce(ORIGINAL, "90802: Bank rejected")
    w.remote.balances["EUR"] = Decimal(eur)
    w.poll()
    return w, w.propose("REPLACE_SAME", "90802", "ben_onboard", "onboarding-v1").id


def test_positive_control_valid_grant_pays_once_then_replay_and_restart_do_not(monkeypatch, tmp_path):
    w, intent = _ready_world("50000.00", "1000", monkeypatch)
    result, error = w.run(intent)
    assert error is None and result is not None and len(w.remote.posts) == 1
    assert w.run(intent)[1] is None and len(w.remote.posts) == 1  # replay returns the stored result
    with pytest.raises(ExecuteError):
        approve_intent(w.conn, intent, approver="finance-approver", as_of=SEEN)  # no regrant


@pytest.mark.parametrize("eur", ["1000.00", "5200.00", "5213.46", "5213.47", "6000.00", "9000.00"])
def test_floor_boundary_uses_real_funding_gate(eur, monkeypatch):
    """The gate budgets a fee at or above the real one, so it may refuse at the exact edge but must
    never pay below principal + actual fee + floor, and a payment never leaves the balance under the floor."""
    w, intent = _ready_world(eur, "1000", monkeypatch)
    before = w.remote.eur()
    result, error = w.run(intent)
    if before < AMOUNT + Decimal("13.47") + 1000:
        assert result is None and error is not None and w.remote.posts == []
    if w.remote.posts:
        assert error is None and before - w.remote.eur() == AMOUNT + Decimal("13.47")
        assert w.remote.eur() >= 1000
    if before >= 9000:
        assert w.remote.posts, "ample balance must pay (positive control)"


def test_unreadable_balance_fails_closed(monkeypatch):
    w, intent = _ready_world("50000.00", "1000", monkeypatch)

    def boom():
        raise ConnectionError("balance unavailable")

    w.remote.current_balances = boom
    result, error = w.run(intent)
    assert result is None and error is not None and w.remote.posts == []


def test_untagged_colleague_payment_blocks_our_replacement(monkeypatch):
    w, intent = _ready_world("50000.00", "1000", monkeypatch)
    w.remote.add_transfer("awx-colleague", status="PAID", amount=str(AMOUNT), reference=INVOICE,
                          beneficiary_id=None, request_id="colleague-hand")
    result, error = w.run(intent)
    assert result is None and error is not None and w.remote.posts == []


def test_colleague_race_after_our_payment_is_held_not_closed(monkeypatch):
    """A human paying after our send is outside our control: the system must hold, never close."""
    w, intent = _ready_world("50000.00", "1000", monkeypatch)
    assert w.run(intent)[1] is None
    w.remote.set_status("awx-new-1", "PAID")
    w.remote.add_transfer("awx-colleague", status="PAID", amount=str(AMOUNT), reference=INVOICE,
                          beneficiary_id=None, request_id="colleague-race")
    w.poll()
    state = w.conn.execute("SELECT state FROM incident WHERE id = ?", (w.incident_id,)).fetchone()["state"]
    status = w.conn.execute("SELECT status FROM obligation WHERE id = ?", (w.obligation_id,)).fetchone()["status"]
    assert state != "CLOSED" and status != "PAID_ONCE"


def test_replacement_closes_then_late_bank_return_reopens(monkeypatch):
    w, intent = _ready_world("50000.00", "1000", monkeypatch)
    assert w.run(intent)[1] is None
    w.remote.set_status("awx-new-1", "PAID")
    w.poll()
    assert w.conn.execute("SELECT state FROM incident WHERE id = ?", (w.incident_id,)).fetchone()["state"] == "CLOSED"
    w.remote.bounce("awx-new-1", "90802: Bank rejected")
    w.poll()
    assert w.conn.execute("SELECT state FROM incident WHERE id = ?", (w.incident_id,)).fetchone()["state"] == "OPEN"

"""Concurrent replace creates one transfer. A timeout is resolved by request id."""

import threading
from decimal import Decimal

import pytest

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import BalanceHistoryItem, BalanceHistoryPage, Transfer, TransferPage
from backend.approvals import AP_CLERK, FINANCE_APPROVER, approve
from backend.execute import intent_terms, open_intent
from backend.ledger import connect, seed_hero
from backend.replace_payment import ReplaceError, replace_payment, replacement_request_id

_SEEN = "2026-11-03T12:00:00+00:00"


def _transfer(transfer_id: str, request_id: str) -> Transfer:
    return Transfer(
        id=transfer_id,
        status="SCHEDULED",
        request_id=request_id,
        transfer_amount=Decimal("4200.00"),
        transfer_currency="EUR",
        beneficiary_id="ben_1",
        reference="INV-2291",
    )


class Api:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.mode = "ok"
        self.found = _transfer("awx-existing", "stl-inv2291-1")
        self.last: dict = {}
        self.failure_reason = "90802: Beneficiary bank returned"

    def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer(
            id=transfer_id,
            status="CANCELLED",
            transfer_amount=Decimal("4200.00"),
            transfer_currency="EUR",
            fee_amount=Decimal("13.47"),
            fee_currency="EUR",
            failure_reason=self.failure_reason,
            reference="INV-2291",
        )

    def balance_history(self, **_kwargs) -> BalanceHistoryPage:
        return BalanceHistoryPage(
            items=(
                BalanceHistoryItem(
                    id="rev",
                    source="synthetic-inv-2291",
                    amount=Decimal("4200.00"),
                    currency="EUR",
                    balance=None,
                    description=None,
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

    def current_balances(self):
        from backend.airwallex.types import Balance

        return [
            Balance("EUR", Decimal("100000.00"), Decimal(0), Decimal(0), Decimal("100000.00"), Decimal(0), account_type="cash"),
            Balance("USD", Decimal("100000.00"), Decimal(0), Decimal(0), Decimal("100000.00"), Decimal(0), account_type="cash"),
        ]

    def create_transfer(self, **kwargs):
        request_id = kwargs["request_id"]
        self.last = kwargs
        self.created.append(request_id)
        if self.mode == "duplicate":
            raise AirwallexError(400, "duplicate_request_id", "")
        if self.mode == "timeout":
            raise TimeoutError
        if self.mode == "refused":
            raise AirwallexError(400, "insufficient_funds", "")
        return _transfer("awx-new", request_id)

    def list_transfers(self, **kwargs):
        if self.mode in {"timeout", "duplicate"} and kwargs.get("request_id") == self.found.request_id:
            return TransferPage(items=(self.found,), page_after=None)
        accepted = getattr(self, "accepted", None)
        if accepted is not None and kwargs.get("request_id") == accepted.request_id:
            return TransferPage(items=(accepted,), page_after=None)
        return TransferPage(items=(), page_after=None)


def _obligation(conn, snapshot: bool = True):
    seed_hero(conn)
    conn.execute(
        "UPDATE transfer_record SET status = 'CANCELLED', request_id = 'original-payment'"
    )
    if snapshot:
        conn.execute(
            """
            INSERT INTO status_snapshot (transfer_record_id, status, source, seen_at)
            SELECT id, 'CANCELLED', 'poll', ? FROM transfer_record
            """,
            (_SEEN,),
        )
    conn.commit()
    obligation_id = conn.execute("SELECT id FROM obligation").fetchone()["id"]
    return obligation_id, _grant(conn, obligation_id)


def _grant(conn, obligation_id: int):
    incident_id = conn.execute(
        "SELECT id FROM incident WHERE obligation_id = ?",
        (obligation_id,),
    ).fetchone()["id"]
    ref = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="REPLACE_SAME",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="ben_1",
        details_hash="onboarding-v1",
        proposer=AP_CLERK,
        failure_code="90802",
        expires_at="2026-11-04T12:00:00+00:00",
        created_at=_SEEN,
    )
    approve(
        conn,
        ref.approval_id,
        intent_terms(conn, ref.id),
        approver=FINANCE_APPROVER,
        used_at=_SEEN,
    )
    return ref


def _pay(conn, api, obligation_id, ref, attempt: int = 1, **extra):
    values = {
        "beneficiary_id": "ben_1",
        "details_hash": "onboarding-v1",
        "transfer_method": "SWIFT",
        "reason": "goods",
        "as_of": _SEEN,
        "intent_id": ref.id,
        "approval_id": ref.approval_id,
        "attempt": attempt,
    }
    values.update(extra)
    return replace_payment(conn, api, obligation_id=obligation_id, **values)


def test_request_id_is_deterministic_and_within_bounds():
    request_id = replacement_request_id("INV-2291", 2)
    assert request_id == "stl-inv2291-2"
    assert 10 <= len(request_id) <= 50


def test_a_recall_does_not_pay_even_with_a_replacement_grant():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    api = Api()
    api.failure_reason = "91001: Recall requested"

    with pytest.raises(ReplaceError) as caught:
        _pay(conn, api, obligation_id, ref)

    assert caught.value.code == "policy"
    assert api.created == []


def test_duplicate_request_id_returns_the_transfer_from_the_lookup():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    api = Api()
    api.mode = "duplicate"
    airwallex_id = _pay(conn, api, obligation_id, ref)
    assert airwallex_id == "awx-existing"
    assert api.created == ["stl-inv2291-1"]
    assert api.last["metadata"] == {"obligation_id": str(obligation_id)}
    rows = conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'"
    ).fetchall()
    assert [row["airwallex_id"] for row in rows] == ["awx-existing"]


def test_a_timeout_is_resolved_by_looking_up_the_request_id():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    api = Api()
    api.mode = "timeout"
    airwallex_id = _pay(conn, api, obligation_id, ref)
    assert airwallex_id == "awx-existing"
    assert api.created == ["stl-inv2291-1"]


def test_concurrent_replace_makes_one_transfer(tmp_path):
    path = tmp_path / "ledger.db"
    setup = connect(path)
    obligation_id, ref = _obligation(setup)
    setup.close()
    api = Api()
    barrier = threading.Barrier(2)
    found: list[str] = []
    errors: list[str] = []

    def worker() -> None:
        conn = connect(path)
        try:
            barrier.wait()
            found.append(_pay(conn, api, obligation_id, ref))
        except ReplaceError as error:
            errors.append(error.code)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert api.created == ["stl-inv2291-1"]
    assert set(found) == {"awx-new"}
    assert errors == [] or errors == ["in_flight"]
    check = connect(path)
    rows = check.execute(
        "SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'"
    ).fetchall()
    assert [row["airwallex_id"] for row in rows] == ["awx-new"]


def _send(**_kwargs):
    raise AssertionError("create should not run")


def test_a_second_replacement_is_refused_without_a_definitive_refusal():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    api = Api()
    _pay(conn, api, obligation_id, ref)
    with pytest.raises(ReplaceError) as caught:
        _pay(conn, api, obligation_id, ref, attempt=2)
    assert caught.value.code == "refused_sequence"
    assert api.created == ["stl-inv2291-1"]


def test_a_timeout_with_no_lookup_stays_in_flight_and_blocks_attempt_two():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    api = Api()
    api.mode = "timeout"
    api.found = _transfer("awx-existing", "other-id")
    with pytest.raises(ReplaceError) as caught:
        _pay(conn, api, obligation_id, ref)
    assert caught.value.code == "unknown"
    status = conn.execute("SELECT status FROM obligation").fetchone()["status"]
    assert status == "IN_FLIGHT"
    with pytest.raises(ReplaceError) as second:
        _pay(conn, api, obligation_id, ref, attempt=2)
    assert second.value.code == "refused_sequence"
    assert api.created == ["stl-inv2291-1"]


def test_a_definitive_refusal_allows_the_next_attempt_until_three():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    api = Api()
    api.mode = "refused"
    for attempt in (1, 2, 3):
        ref = _grant(conn, obligation_id)
        with pytest.raises(ReplaceError) as caught:
            _pay(conn, api, obligation_id, ref, attempt=attempt)
        assert caught.value.code == "refused"
    with pytest.raises(ReplaceError) as caught:
        _pay(conn, api, obligation_id, ref, attempt=4)
    assert caught.value.code == "max_attempts"
    assert api.created == ["stl-inv2291-1", "stl-inv2291-2", "stl-inv2291-3"]


def test_nothing_is_resent_past_six_days():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    transfer_id = conn.execute(
        "SELECT id FROM transfer_record WHERE request_id = 'original-payment'"
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO status_snapshot (
            transfer_record_id, status, source, seen_at
        ) VALUES (?, 'CANCELLED', 'poll', '2026-11-01T00:00:00+00:00')
        """,
        (transfer_id,),
    )
    conn.commit()
    api = Api()
    with pytest.raises(ReplaceError) as caught:
        replace_payment(
            conn,
            api,
            obligation_id=obligation_id,
            attempt=1,
            as_of="2026-11-08T00:00:00+00:00",
            intent_id=ref.id,
            approval_id=ref.approval_id,
            beneficiary_id="ben_1",
            details_hash="onboarding-v1",
            transfer_method="SWIFT",
            reason="goods",
        )
    assert caught.value.code == "too_late"
    assert api.created == []


def test_no_snapshot_means_no_send_and_the_intent_and_grant_stay_unspent():
    conn = connect()
    obligation_id, ref = _obligation(conn, snapshot=False)
    assert conn.execute("SELECT COUNT(*) FROM status_snapshot").fetchone()[0] == 0
    grant_before = tuple(
        conn.execute("SELECT * FROM approval WHERE id = ?", (ref.approval_id,)).fetchone()
    )
    api = Api()
    with pytest.raises(ReplaceError) as caught:
        _pay(conn, api, obligation_id, ref)
    assert caught.value.code == "too_late"
    assert api.created == []
    intent = conn.execute(
        "SELECT state, request_id FROM execution_intent WHERE id = ?", (ref.id,)
    ).fetchone()
    assert intent["state"] == "proposed"
    assert intent["request_id"] is None
    grant_after = tuple(
        conn.execute("SELECT * FROM approval WHERE id = ?", (ref.approval_id,)).fetchone()
    )
    assert grant_after == grant_before
    # The same intent and grant still work once evidence exists.
    transfer_id = conn.execute(
        "SELECT id FROM transfer_record WHERE request_id = 'original-payment'"
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO status_snapshot (transfer_record_id, status, source, seen_at)
        VALUES (?, 'CANCELLED', 'poll', ?)
        """,
        (transfer_id, _SEEN),
    )
    conn.commit()
    _pay(conn, api, obligation_id, ref)
    assert api.created == ["stl-inv2291-1"]


def test_the_six_day_cutoff_uses_the_clock_and_ignores_a_later_snapshot():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    transfer_id = conn.execute(
        "SELECT id FROM transfer_record WHERE request_id = 'original-payment'"
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO status_snapshot (transfer_record_id, status, source, seen_at)
        VALUES (?, 'CANCELLED', 'poll', '2020-01-01T00:00:00+00:00')
        """,
        (transfer_id,),
    )
    conn.commit()
    api = Api()
    with pytest.raises(ReplaceError) as caught:
        replace_payment(
            conn,
            api,
            obligation_id=obligation_id,
            attempt=1,
            intent_id=ref.id,
            approval_id=ref.approval_id,
            beneficiary_id="ben_1",
            details_hash="onboarding-v1",
            transfer_method="SWIFT",
            reason="goods",
        )
    assert caught.value.code == "too_late"
    assert api.created == []
    conn.execute(
        """
        INSERT INTO status_snapshot (transfer_record_id, status, source, seen_at)
        VALUES (?, 'CANCELLED', 'poll', '2026-11-07T00:00:00+00:00')
        """,
        (transfer_id,),
    )
    conn.commit()
    with pytest.raises(ReplaceError) as later:
        replace_payment(
            conn,
            api,
            obligation_id=obligation_id,
            attempt=1,
            as_of="2026-11-08T00:00:00+00:00",
            intent_id=ref.id,
            approval_id=ref.approval_id,
            beneficiary_id="ben_1",
            details_hash="onboarding-v1",
            transfer_method="SWIFT",
            reason="goods",
        )
    assert later.value.code == "too_late"


def test_an_ambiguous_external_duplicate_blocks_replacement():
    conn = connect()
    obligation_id, ref = _obligation(conn)
    conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, 'ambiguous:awx-x', 'AMBIGUOUS')",
        (obligation_id,),
    )
    conn.commit()
    api = Api()
    with pytest.raises(ReplaceError) as caught:
        _pay(conn, api, obligation_id, ref)
    assert caught.value.code == "ambiguous"
    assert api.created == []


_TABLES = ("execution_intent", "transfer_record", "approval", "audit_event", "obligation")


def _state(conn):
    return {
        table: [tuple(row) for row in conn.execute("SELECT * FROM " + table + " ORDER BY 1")]
        for table in _TABLES
    }


def _pending(conn):
    obligation_id, ref = _obligation(conn)
    api = Api()
    api.mode = "timeout"
    api.found = _transfer("awx-existing", "other-id")
    with pytest.raises(ReplaceError):
        _pay(conn, api, obligation_id, ref)
    api.found = _transfer("awx-existing", "stl-inv2291-1")
    return obligation_id, ref, api


def _completed(conn):
    obligation_id, ref = _obligation(conn)
    api = Api()
    _pay(conn, api, obligation_id, ref)
    return obligation_id, ref, api


def _assert_refused_without_writes(conn, api, obligation_id, ref, **kwargs):
    before = _state(conn)
    created = list(api.created)
    with pytest.raises(ReplaceError):
        _pay(conn, api, obligation_id, ref, **kwargs)
    assert _state(conn) == before
    assert api.created == created


@pytest.mark.parametrize("make", [_pending, _completed], ids=["pending", "completed"])
def test_a_different_intent_cannot_recover_an_attempt_it_does_not_own(make):
    conn = connect()
    obligation_id, _ref, api = make(conn)
    other = _grant(conn, obligation_id)
    _assert_refused_without_writes(conn, api, obligation_id, other)


@pytest.mark.parametrize("make", [_pending, _completed], ids=["pending", "completed"])
def test_the_owner_intent_with_another_grant_cannot_recover(make):
    conn = connect()
    obligation_id, ref, api = make(conn)
    other = _grant(conn, obligation_id)
    _assert_refused_without_writes(
        conn, api, obligation_id, ref, approval_id=other.approval_id
    )


@pytest.mark.parametrize("make", [_pending, _completed], ids=["pending", "completed"])
def test_the_owner_with_its_spent_grant_still_recovers(make):
    conn = connect()
    obligation_id, ref, api = make(conn)
    expected = "awx-existing" if make is _pending else "awx-new"
    assert _pay(conn, api, obligation_id, ref) == expected

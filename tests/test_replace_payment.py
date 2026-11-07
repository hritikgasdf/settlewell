"""Concurrent replace creates one transfer. A timeout is resolved by request id."""

import threading
from decimal import Decimal

import pytest

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import Transfer, TransferPage
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
            failure_reason=self.failure_reason,
            reference="INV-2291",
        )

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
        if kwargs.get("request_id") == self.found.request_id:
            return TransferPage(items=(self.found,), page_after=None)
        return TransferPage(items=(), page_after=None)


def _obligation(conn):
    seed_hero(conn)
    conn.execute(
        "UPDATE transfer_record SET status = 'CANCELLED', request_id = 'original-payment'"
    )
    conn.commit()
    obligation_id = conn.execute("SELECT id FROM obligation").fetchone()["id"]
    _grant(conn, obligation_id)
    return obligation_id


def _grant(conn, obligation_id: int) -> None:
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
        created_at=_SEEN,
    )
    approve(
        conn,
        ref.approval_id,
        intent_terms(conn, ref.id),
        approver=FINANCE_APPROVER,
        used_at=_SEEN,
    )


_ARGS = {
    "beneficiary_id": "ben_1",
    "details_hash": "onboarding-v1",
    "transfer_method": "SWIFT",
    "reason": "goods",
}


def test_request_id_is_deterministic_and_within_bounds():
    request_id = replacement_request_id("INV-2291", 2)
    assert request_id == "stl-inv2291-2"
    assert 10 <= len(request_id) <= 50


def test_a_recall_does_not_pay_even_with_a_replacement_grant():
    conn = connect()
    obligation_id = _obligation(conn)
    api = Api()
    api.failure_reason = "91001: Recall requested"

    with pytest.raises(ReplaceError) as caught:
        replace_payment(conn, api, obligation_id=obligation_id, attempt=1, **_ARGS)

    assert caught.value.code == "policy"
    assert api.created == []


def test_duplicate_request_id_returns_the_transfer_from_the_lookup():
    conn = connect()
    obligation_id = _obligation(conn)
    api = Api()
    api.mode = "duplicate"
    airwallex_id = replace_payment(
        conn,
        api,
        obligation_id=obligation_id,
        attempt=1,
        beneficiary_id="ben_1",
        details_hash="onboarding-v1",
        transfer_method="SWIFT",
        reason="goods",
    )
    assert airwallex_id == "awx-existing"
    assert api.created == ["stl-inv2291-1"]
    assert api.last["metadata"] == {"obligation_id": str(obligation_id)}
    rows = conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'"
    ).fetchall()
    assert [row["airwallex_id"] for row in rows] == ["awx-existing"]


def test_a_timeout_is_resolved_by_looking_up_the_request_id():
    conn = connect()
    obligation_id = _obligation(conn)
    api = Api()
    api.mode = "timeout"
    airwallex_id = replace_payment(
        conn,
        api,
        obligation_id=obligation_id,
        attempt=1,
        beneficiary_id="ben_1",
        details_hash="onboarding-v1",
        transfer_method="SWIFT",
        reason="goods",
    )
    assert airwallex_id == "awx-existing"
    assert api.created == ["stl-inv2291-1"]


def test_concurrent_replace_makes_one_transfer(tmp_path):
    path = tmp_path / "ledger.db"
    setup = connect(path)
    obligation_id = _obligation(setup)
    setup.close()
    api = Api()
    barrier = threading.Barrier(2)
    found: list[str] = []

    def worker() -> None:
        conn = connect(path)
        try:
            barrier.wait()
            found.append(
                replace_payment(
                    conn,
                    api,
                    obligation_id=obligation_id,
                    attempt=1,
                    beneficiary_id="ben_1",
                    details_hash="onboarding-v1",
                    transfer_method="SWIFT",
                    reason="goods",
                )
            )
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert api.created == ["stl-inv2291-1"]
    assert found == ["awx-new", "awx-new"]
    check = connect(path)
    rows = check.execute(
        "SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'"
    ).fetchall()
    assert [row["airwallex_id"] for row in rows] == ["awx-new"]


def _send(**_kwargs):
    raise AssertionError("create should not run")


def test_a_second_replacement_is_refused_without_a_definitive_refusal():
    conn = connect()
    obligation_id = _obligation(conn)
    api = Api()
    replace_payment(
        conn,
        api,
        obligation_id=obligation_id,
        attempt=1,
        beneficiary_id="ben_1",
        details_hash="onboarding-v1",
        transfer_method="SWIFT",
        reason="goods",
    )
    with pytest.raises(ReplaceError) as caught:
        replace_payment(conn, api, obligation_id=obligation_id, attempt=2, **_ARGS)
    assert caught.value.code == "refused_sequence"
    assert api.created == ["stl-inv2291-1"]


def test_a_timeout_with_no_lookup_stays_in_flight_and_blocks_attempt_two():
    conn = connect()
    obligation_id = _obligation(conn)
    api = Api()
    api.mode = "timeout"
    api.found = _transfer("awx-existing", "other-id")
    with pytest.raises(ReplaceError) as caught:
        replace_payment(conn, api, obligation_id=obligation_id, attempt=1, **_ARGS)
    assert caught.value.code == "unknown"
    status = conn.execute("SELECT status FROM obligation").fetchone()["status"]
    assert status == "IN_FLIGHT"
    with pytest.raises(ReplaceError) as second:
        replace_payment(conn, api, obligation_id=obligation_id, attempt=2, **_ARGS)
    assert second.value.code == "refused_sequence"
    assert api.created == ["stl-inv2291-1"]


def test_a_definitive_refusal_allows_the_next_attempt_until_three():
    conn = connect()
    obligation_id = _obligation(conn)
    api = Api()
    api.mode = "refused"
    for attempt in (1, 2, 3):
        _grant(conn, obligation_id)
        with pytest.raises(ReplaceError) as caught:
            replace_payment(conn, api, obligation_id=obligation_id, attempt=attempt, **_ARGS)
        assert caught.value.code == "refused"
    with pytest.raises(ReplaceError) as caught:
        replace_payment(conn, api, obligation_id=obligation_id, attempt=4, **_ARGS)
    assert caught.value.code == "max_attempts"
    assert api.created == ["stl-inv2291-1", "stl-inv2291-2", "stl-inv2291-3"]


def test_nothing_is_resent_past_six_days():
    conn = connect()
    obligation_id = _obligation(conn)
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
            **_ARGS,
        )
    assert caught.value.code == "too_late"
    assert api.created == []

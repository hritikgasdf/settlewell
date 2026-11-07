"""A scheduled duplicate cancels at zero cost. An unsupported update is re-read."""

import sqlite3

import pytest

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import CancelResult, Transfer
from backend.approvals import AP_CLERK, FINANCE_APPROVER, approve
from backend.execute import intent_terms, open_intent
from backend.ledger import connect, insert_external_transfer, seed_hero
from backend.stop_duplicate import StopDuplicateError, stop_duplicate


class DuplicateApi:
    def __init__(self, status: str = "SCHEDULED", *, fail: str | None = None) -> None:
        self.status = status
        self.fail = fail
        self.cancels = 0

    def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer(
            id=transfer_id,
            status=self.status,
            transfer_amount="4200.00",
            transfer_currency="EUR",
            beneficiary_id="ben_colleague",
            reference="INV-2291",
        )

    def cancel_transfer(self, _transfer_id: str) -> CancelResult:
        self.cancels += 1
        if self.fail:
            raise AirwallexError(400, self.fail, "")
        self.status = "CANCELLED"
        return CancelResult(ok=True, transfer=None)


def _ready(conn):
    incident_id = seed_hero(conn)
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    inserted = insert_external_transfer(
        conn,
        airwallex_id="awx-dup",
        request_id="colleague-friday",
        obligation_id=obligation_id,
        amount="4200.00",
        currency="EUR",
        status="SCHEDULED",
        reference="INV-2291",
    )
    conn.execute(
        "UPDATE transfer_record SET details_hash = ? WHERE id = ?",
        ("colleague-v1", inserted),
    )
    conn.commit()
    ref = open_intent(
        conn,
        kind="cancel",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="STOP_DUPLICATE",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="ben_colleague",
        details_hash="colleague-v1",
        proposer=AP_CLERK,
        target_id=str(inserted),
        expires_at="2026-11-04T12:00:00+00:00",
        created_at="2026-11-03T12:00:00+00:00",
    )
    approve(
        conn,
        ref.approval_id,
        intent_terms(conn, ref.id),
        approver=FINANCE_APPROVER,
        used_at="2026-11-03T12:00:00+00:00",
    )
    return ref.approval_id, inserted


def test_an_approved_scheduled_duplicate_cancels_with_no_ledger_impact():
    conn = connect()
    api = DuplicateApi()
    approval_id, transfer_id = _ready(conn)
    before_amount = conn.execute("SELECT amount FROM obligation").fetchone()["amount"]
    before_balances = conn.execute("SELECT COUNT(*) AS n FROM balance_snapshot").fetchone()["n"]

    result = stop_duplicate(
        conn, api, approval_id=approval_id, transfer_record_id=transfer_id, as_of="2026-11-03T12:00:00+00:00"
    )

    assert result.cancelled is True
    assert result.status == "CANCELLED"
    assert result.reread is False
    assert api.cancels == 1
    stored = conn.execute("SELECT status, fee FROM transfer_record WHERE id = ?", (transfer_id,)).fetchone()
    assert stored["status"] == "CANCELLED"
    assert stored["fee"] is None
    assert conn.execute("SELECT amount FROM obligation").fetchone()["amount"] == before_amount
    after_balances = conn.execute("SELECT COUNT(*) AS n FROM balance_snapshot").fetchone()["n"]
    assert after_balances == before_balances


def test_cancel_is_refused_without_an_approval():
    conn = connect()
    api = DuplicateApi()
    approval_id, transfer_id = _ready(conn)
    conn.execute("UPDATE approval SET used_at = NULL, approver = NULL WHERE id = ?", (approval_id,))
    conn.commit()
    with pytest.raises(StopDuplicateError) as caught:
        stop_duplicate(
            conn,
            api,
            approval_id=approval_id,
            transfer_record_id=transfer_id,
            as_of="2026-11-03T12:00:00+00:00",
        )
    assert caught.value.code == "not_approved"
    assert api.cancels == 0


def test_update_unsupported_is_reread_and_not_marked_cancelled():
    conn = connect()

    class Moved(DuplicateApi):
        def __init__(self) -> None:
            super().__init__(fail="update_unsupported")
            self.reads = 0

        def get_transfer(self, transfer_id: str) -> Transfer:
            self.reads += 1
            self.status = "SCHEDULED" if self.reads == 1 else "SENT"
            return super().get_transfer(transfer_id)

    api = Moved()
    approval_id, transfer_id = _ready(conn)
    result = stop_duplicate(
        conn, api, approval_id=approval_id, transfer_record_id=transfer_id, as_of="2026-11-03T12:00:00+00:00"
    )
    assert result.cancelled is False
    assert result.reread is True
    assert result.status == "SENT"
    assert api.cancels == 1
    stored = conn.execute("SELECT status FROM transfer_record WHERE id = ?", (transfer_id,)).fetchone()
    assert stored["status"] == "SENT"


def test_a_processing_duplicate_is_refused_and_the_grant_stays_open():
    conn = connect()
    api = DuplicateApi(status="PROCESSING")
    approval_id, transfer_id = _ready(conn)

    result = stop_duplicate(
        conn,
        api,
        approval_id=approval_id,
        transfer_record_id=transfer_id,
        as_of="2026-11-03T12:00:00+00:00",
    )

    assert result.cancelled is False
    assert result.status == "PROCESSING"
    assert result.reread is True
    assert api.cancels == 0
    state = conn.execute("SELECT state FROM execution_intent").fetchone()["state"]
    assert state == "proposed"


class _FailingCommit:
    def __init__(self, conn, *, fail_on: int) -> None:
        self._conn = conn
        self.fail_on = fail_on
        self.commits = 0
        self.armed = True

    def commit(self) -> None:
        self.commits += 1
        if self.armed and self.commits == self.fail_on:
            raise sqlite3.OperationalError("disk")
        self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_cancel_recovers_after_the_local_write_fails():
    conn = connect()
    api = DuplicateApi()
    approval_id, transfer_id = _ready(conn)
    proxy = _FailingCommit(conn, fail_on=2)
    with pytest.raises(sqlite3.OperationalError):
        stop_duplicate(
            proxy,
            api,
            approval_id=approval_id,
            transfer_record_id=transfer_id,
            as_of="2026-11-03T12:00:00+00:00",
        )
    proxy.rollback()
    proxy.armed = False

    result = stop_duplicate(
        proxy,
        api,
        approval_id=approval_id,
        transfer_record_id=transfer_id,
        as_of="2026-11-03T12:00:00+00:00",
    )

    assert result.cancelled is True
    assert result.status == "CANCELLED"
    assert api.cancels == 1

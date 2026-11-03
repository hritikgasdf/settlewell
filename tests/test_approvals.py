"""Changed details void an approval. A second click is rejected. No network."""

import pytest

from backend.approvals import (
    AP_CLERK,
    FINANCE_APPROVER,
    ApprovalError,
    ApprovalTerms,
    approve,
    propose,
)
from backend.ledger import connect, seed_hero

_TERMS = ApprovalTerms(
    action="REPLACE_NEW_DETAILS",
    amount="4200.00",
    currency="EUR",
    beneficiary_id="ben_1",
    details_hash="onboarding-v1",
    quote_id="quo_1",
    evidence_hashes=("sha-a", "sha-b"),
)


def _incident() -> tuple:
    conn = connect()
    incident_id = seed_hero(conn)
    return conn, incident_id


def test_a_second_click_is_rejected():
    conn, incident_id = _incident()
    approval_id = propose(conn, incident_id, _TERMS, proposer=AP_CLERK)
    approve(conn, approval_id, _TERMS, approver=FINANCE_APPROVER, used_at="2026-11-03T12:00:00+00:00")
    with pytest.raises(ApprovalError) as caught:
        approve(conn, approval_id, _TERMS, approver=FINANCE_APPROVER, used_at="2026-11-03T12:00:01+00:00")
    assert caught.value.code == "used"
    row = conn.execute("SELECT used_at FROM approval WHERE id = ?", (approval_id,)).fetchone()
    assert row["used_at"] == "2026-11-03T12:00:00+00:00"


def test_changed_details_void_the_approval():
    conn, incident_id = _incident()
    approval_id = propose(conn, incident_id, _TERMS, proposer=AP_CLERK)
    changed = ApprovalTerms(
        action=_TERMS.action,
        amount=_TERMS.amount,
        currency=_TERMS.currency,
        beneficiary_id=_TERMS.beneficiary_id,
        details_hash="callback-v2",
        quote_id=_TERMS.quote_id,
        evidence_hashes=_TERMS.evidence_hashes,
    )
    with pytest.raises(ApprovalError) as caught:
        approve(conn, approval_id, changed, approver=FINANCE_APPROVER, used_at="2026-11-03T12:00:00+00:00")
    assert caught.value.code == "void"
    row = conn.execute("SELECT used_at FROM approval WHERE id = ?", (approval_id,)).fetchone()
    assert row["used_at"] is None
    approve(conn, approval_id, _TERMS, approver=FINANCE_APPROVER, used_at="2026-11-03T12:00:02+00:00")


def test_a_different_quote_or_evidence_set_voids_the_approval():
    conn, incident_id = _incident()
    approval_id = propose(conn, incident_id, _TERMS, proposer=AP_CLERK)
    other_quote = ApprovalTerms(
        action=_TERMS.action,
        amount=_TERMS.amount,
        currency=_TERMS.currency,
        beneficiary_id=_TERMS.beneficiary_id,
        details_hash=_TERMS.details_hash,
        quote_id="quo_2",
        evidence_hashes=_TERMS.evidence_hashes,
    )
    dropped_evidence = ApprovalTerms(
        action=_TERMS.action,
        amount=_TERMS.amount,
        currency=_TERMS.currency,
        beneficiary_id=_TERMS.beneficiary_id,
        details_hash=_TERMS.details_hash,
        quote_id=_TERMS.quote_id,
        evidence_hashes=("sha-a",),
    )
    with pytest.raises(ApprovalError) as quote_error:
        approve(conn, approval_id, other_quote, approver=FINANCE_APPROVER, used_at="2026-11-03T12:00:00+00:00")
    with pytest.raises(ApprovalError) as evidence_error:
        approve(
            conn,
            approval_id,
            dropped_evidence,
            approver=FINANCE_APPROVER,
            used_at="2026-11-03T12:00:00+00:00",
        )
    assert quote_error.value.code == "void"
    assert evidence_error.value.code == "void"


def test_a_concurrent_click_loses_when_the_row_is_taken():
    conn, incident_id = _incident()
    approval_id = propose(conn, incident_id, _TERMS, proposer=AP_CLERK)
    winner = "2026-11-03T11:59:00+00:00"

    def other_click() -> None:
        conn.execute(
            "UPDATE approval SET used_at = ?, approver = ? WHERE id = ? AND used_at IS NULL",
            (winner, FINANCE_APPROVER, approval_id),
        )

    with pytest.raises(ApprovalError) as caught:
        approve(
            conn,
            approval_id,
            _TERMS,
            approver=FINANCE_APPROVER,
            used_at="2026-11-03T12:00:00+00:00",
            before_update=other_click,
        )
    assert caught.value.code == "used"
    row = conn.execute("SELECT used_at FROM approval WHERE id = ?", (approval_id,)).fetchone()
    assert row["used_at"] == winner


def test_the_proposer_cannot_approve_their_own_grant():
    conn, incident_id = _incident()
    approval_id = propose(conn, incident_id, _TERMS, proposer=AP_CLERK)
    with pytest.raises(ApprovalError) as caught:
        approve(conn, approval_id, _TERMS, approver=AP_CLERK, used_at="2026-11-03T12:00:00+00:00")
    assert caught.value.code == "self"
    row = conn.execute("SELECT used_at FROM approval WHERE id = ?", (approval_id,)).fetchone()
    assert row["used_at"] is None
    approve(conn, approval_id, _TERMS, approver=FINANCE_APPROVER, used_at="2026-11-03T12:00:00+00:00")

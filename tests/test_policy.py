"""One assertion per published failure code, plus the three v1 gates."""

import pytest

from backend.policy import FAILURE_ROWS, Action, code_from_reason, decide

_IN_FLIGHT = ("SCHEDULED", "PROCESSING", "SENT", "OVERDUE", "FAILED")


def test_every_published_failure_code_is_unique():
    codes = [row.code for row in FAILURE_ROWS]
    assert len(codes) == len(set(codes))
    assert len(codes) == 29


@pytest.mark.parametrize("row", FAILURE_ROWS, ids=lambda row: row.code)
def test_cancelled_transfer_follows_its_failure_code_row(row):
    decision = decide("CANCELLED", failure_code=row.code, claim="where")
    assert decision.action is row.action
    assert decision.rule_id == row.rule_id
    assert decision.proof_allowed is False


@pytest.mark.parametrize("status", _IN_FLIGHT)
@pytest.mark.parametrize("code", ("90802", "90701", "91001"))
def test_no_replacement_while_the_transfer_is_in_flight(status, code):
    decision = decide(status, failure_code=code)
    assert decision.action is Action.WAIT
    assert decision.rule_id == "R-NO-REPLACE-IN-FLIGHT"
    assert decision.action is not Action.REPLACE_SAME
    assert decision.action is not Action.REPLACE_NEW_DETAILS


def test_proof_only_while_sent_or_paid():
    sent = decide("SENT", claim="proof")
    paid = decide("PAID", claim="proof")
    cancelled = decide("CANCELLED", failure_code="90802", claim="proof")
    assert sent.action is Action.PROOF and sent.proof_allowed is True
    assert paid.action is Action.PROOF and paid.proof_allowed is True
    assert cancelled.action is not Action.PROOF
    assert cancelled.proof_allowed is False


def test_stop_duplicate_only_while_scheduled():
    scheduled = decide("SENT", claim="duplicate", duplicate_status="SCHEDULED")
    sent = decide("SCHEDULED", claim="duplicate", duplicate_status="SENT")
    processing = decide("SCHEDULED", claim="duplicate", duplicate_status="PROCESSING")
    assert scheduled.action is Action.STOP_DUPLICATE
    assert scheduled.rule_id == "R-STOP-DUPLICATE-SCHEDULED"
    assert sent.action is Action.ESCALATE
    assert sent.rule_id == "R-NO-STOP-NOT-SCHEDULED"
    assert processing.action is not Action.STOP_DUPLICATE


def test_account_closed_asks_for_new_details_and_recall_escalates():
    closed = decide("CANCELLED", failure_code="90701")
    returned = decide("CANCELLED", failure_code="90802")
    recall = decide("CANCELLED", failure_code="91001")
    assert closed.action is Action.REPLACE_NEW_DETAILS
    assert returned.action is Action.REPLACE_SAME
    assert recall.action is Action.ESCALATE


def test_new_bank_details_on_a_bank_return_are_not_a_same_beneficiary_replace():
    decision = decide("CANCELLED", failure_code="90802", claim="new_details")
    assert decision.action is Action.REPLACE_NEW_DETAILS
    assert decision.rule_id == "R-NEW-DETAILS-UNVERIFIED"


def test_unknown_or_missing_code_escalates_and_does_not_prove():
    missing = decide("CANCELLED", failure_code=None, claim="proof")
    unknown = decide("CANCELLED", failure_code="00000", claim="proof")
    assert missing.rule_id == "R-CANCELLED-NO-CODE"
    assert unknown.rule_id == "R-UNKNOWN-CODE"
    assert missing.proof_allowed is False
    assert unknown.proof_allowed is False


def test_code_is_read_from_the_pinned_failure_reason():
    assert code_from_reason("90701: Account closed") == "90701"
    assert code_from_reason(None) is None
    decision = decide("CANCELLED", failure_code=code_from_reason("90802: Beneficiary bank returned"))
    assert decision.action is Action.REPLACE_SAME

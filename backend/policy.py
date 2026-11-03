"""Deterministic payment-incident policy. Codes come from Airwallex's failure-reasons table.

Decisions are status × failure code × claim. Nothing here calls Airwallex or chooses an amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    WAIT = "WAIT"
    PROOF = "PROOF"
    TRACE = "TRACE"
    STOP_DUPLICATE = "STOP_DUPLICATE"
    REPLACE_SAME = "REPLACE_SAME"
    REPLACE_NEW_DETAILS = "REPLACE_NEW_DETAILS"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class FailureRow:
    code: str
    description: str
    action: Action

    @property
    def rule_id(self) -> str:
        return f"R-{self.action.value}-{self.code}"


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_id: str
    proof_allowed: bool


_IN_FLIGHT = frozenset({"SCHEDULED", "PROCESSING", "SENT", "OVERDUE", "FAILED", "IN_APPROVAL"})
_PROOF_OK = frozenset({"SENT", "PAID"})


def _rows(*pairs: tuple[str, str], action: Action) -> tuple[FailureRow, ...]:
    return tuple(FailureRow(code, description, action) for code, description in pairs)


# Published failure reasons, grouped by the action Settlewell will propose once CANCELLED.
# 90802 is the same-beneficiary replacement. 901–904 and 907 need corrected account details.
# Recalls, China-local errors, system errors, and unknown codes escalate.
_NEW_DETAILS = _rows(
    ("90101", "Invalid account name/number"),
    ("90201", "Invalid bank/branch code"),
    ("90202", "Invalid SWIFT/BIC code"),
    ("90203", "Invalid correspondent bank information"),
    ("90204", "Invalid bank information"),
    ("90301", "Beneficiary name mismatch"),
    ("90302", "Account currency mismatch"),
    ("90401", "Invalid beneficiary details"),
    ("90402", "Invalid special character"),
    ("90701", "Account closed"),
    ("90702", "Account inactive or dormant"),
    ("90703", "Account under restriction"),
    action=Action.REPLACE_NEW_DETAILS,
)
_SAME_BENEFICIARY = _rows(
    ("90802", "Beneficiary bank returned"),
    action=Action.REPLACE_SAME,
)
_ESCALATE = _rows(
    ("90501", "Channel policy"),
    ("90502", "TM suspended"),
    ("90604", "Insufficient balance"),
    ("90801", "Beneficiary requested"),
    ("90901", "China local transfer beneficiary validation error"),
    ("90902", "China local transfer order error"),
    ("90903", "Beneficiary account type not supported"),
    ("90904", "Chinese company registration number error"),
    ("91001", "Recall requested"),
    ("91002", "Client requested"),
    ("91101", "Invalid payment purpose"),
    ("91201", "China local transfer processing failure"),
    ("91401", "System error"),
    ("91402", "Channel timeout"),
    ("99901", "Unable to apply"),
    ("99902", "Other"),
    action=Action.ESCALATE,
)

FAILURE_ROWS: tuple[FailureRow, ...] = _NEW_DETAILS + _SAME_BENEFICIARY + _ESCALATE
FAILURE_BY_CODE: dict[str, FailureRow] = {row.code: row for row in FAILURE_ROWS}


def decide(
    status: str,
    *,
    failure_code: str | None = None,
    claim: str = "where",
    duplicate_status: str | None = None,
) -> Decision:
    """Return the next action. Proof is allowed only while the transfer is SENT or PAID."""
    normalized = status.upper()
    code = failure_code.strip() if failure_code else None
    if claim == "duplicate":
        target = (duplicate_status or normalized).upper()
        if target == "SCHEDULED":
            return Decision(Action.STOP_DUPLICATE, "R-STOP-DUPLICATE-SCHEDULED", False)
        return Decision(Action.ESCALATE, "R-NO-STOP-NOT-SCHEDULED", False)
    if claim == "proof" and normalized in _PROOF_OK:
        return Decision(Action.PROOF, f"R-PROOF-{normalized}", True)
    if claim == "trace" and normalized == "SENT":
        return Decision(Action.TRACE, "R-TRACE-SENT", True)
    if normalized in _IN_FLIGHT or normalized == "PAID":
        proof_ok = normalized in _PROOF_OK
        rule = "R-WAIT-PAID" if normalized == "PAID" else "R-NO-REPLACE-IN-FLIGHT"
        return Decision(Action.WAIT, rule, proof_ok)
    if normalized == "CANCELLED":
        return _cancelled(code, claim)
    return Decision(Action.ESCALATE, "R-UNKNOWN-STATUS", False)


def code_from_reason(failure_reason: str | None) -> str | None:
    """Read the numeric code from a pinned-version reason such as '90701: Account closed'."""
    if not failure_reason or ":" not in failure_reason:
        return None
    code = failure_reason.split(":", 1)[0].strip()
    return code or None


def _cancelled(code: str | None, claim: str) -> Decision:
    if not code:
        return Decision(Action.ESCALATE, "R-CANCELLED-NO-CODE", False)
    row = FAILURE_BY_CODE.get(code)
    if row is None:
        return Decision(Action.ESCALATE, "R-UNKNOWN-CODE", False)
    if claim == "new_details" and row.action is Action.REPLACE_SAME:
        return Decision(Action.REPLACE_NEW_DETAILS, "R-NEW-DETAILS-UNVERIFIED", False)
    return Decision(row.action, row.rule_id, False)

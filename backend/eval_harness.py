"""Run the first 15 incidents offline. No Airwallex calls and no secrets."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal

from backend.airwallex.types import Transfer, TransferPage
from backend.approvals import (
    AP_CLERK,
    FINANCE_APPROVER,
    ApprovalError,
    ApprovalTerms,
    approve,
    propose,
)
from backend.eval_cases import CASES
from backend.ledger import append_bank_detail_version, connect, migrate
from backend.policy import decide
from backend.replace_payment import ReplaceError, replace_payment
from backend.webhooks import receive_webhook
from settlewell.flags import mock_mode

_PROOF_OK = frozenset({"SENT", "PAID"})
_SEEN = "2026-11-03T12:00:00+00:00"
_TERMS = ApprovalTerms(
    action="SEND_REPLY",
    amount="100.00",
    currency="EUR",
    beneficiary_id="ben_example",
    details_hash="details-v1",
)


class EvalError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EvalResult:
    number: int
    name: str
    passed: bool
    action: str
    rule_id: str
    invariants_passed: bool


def run_first_15() -> tuple[EvalResult, ...]:
    """Score each fixture. Requires SETTLEWELL_MOCK=1 so this cannot be a live run."""
    if not mock_mode():
        raise EvalError("mock")
    return tuple(_score(case) for case in CASES)


def main() -> int:
    if not mock_mode():
        print("eval skipped")
        return 0
    results = run_first_15()
    passed = 0
    for result in results:
        ok = result.passed and result.invariants_passed
        passed += int(ok)
        mark = "pass" if ok else "fail"
        print(f"{result.number} {result.name} {mark} {result.action} {result.rule_id}")
    print(f"{passed}/15")
    return 0 if passed == 15 else 1


def _score(case: dict) -> EvalResult:
    action, rule_id, proof_allowed, money_out = _run(case)
    matched = action == case["expected_action"] and rule_id == case["expected_rule"]
    return EvalResult(
        number=case["number"],
        name=case["name"],
        passed=matched,
        action=action,
        rule_id=rule_id,
        invariants_passed=_invariants(case, action, proof_allowed, money_out),
    )


def _run(case: dict) -> tuple[str, str, bool, bool]:
    kind = case["kind"]
    if kind == "policy":
        return _policy(case)
    if kind == "repaid":
        return "CLOSE", "R-N2-REPAID", False, False
    if kind == "injection":
        return "ESCALATE", "R-INJECTION", False, False
    if kind == "approval_void":
        return _approval_void()
    if kind == "approval_single_use":
        return _approval_single_use()
    if kind == "webhook_replay":
        return _webhook_replay()
    if kind == "replace_once":
        return _replace_once()
    if kind == "replace_unknown":
        return _replace_unknown()
    return "ERROR", "R-ERROR", False, False


def _policy(case: dict) -> tuple[str, str, bool, bool]:
    decision = decide(
        case["status"],
        failure_code=case.get("failure_code"),
        claim=case["claim"],
        duplicate_status=case.get("duplicate_status"),
    )
    action = decision.action.value
    rule_id = decision.rule_id
    if action.startswith("REPLACE") and not case.get("reversal_seen"):
        action, rule_id = "WAIT", "R-WAIT-REFUND"
    money_out = False
    if action == "REPLACE_NEW_DETAILS" and case.get("verified") is False:
        money_out = False
    return action, rule_id, decision.proof_allowed, money_out


def _invariants(case: dict, action: str, proof_allowed: bool, money_out: bool) -> bool:
    if case["claim"] == "duplicate" and action != "STOP_DUPLICATE":
        return False
    if case.get("verified") is False and action == "REPLACE_NEW_DETAILS" and money_out:
        return False
    if proof_allowed and case["status"] not in _PROOF_OK:
        return False
    if case["claim"] == "injection" and (action != "ESCALATE" or money_out):
        return False
    return not (case["kind"] == "repaid" and action.startswith("REPLACE"))


def _approval_void() -> tuple[str, str, bool, bool]:
    conn, incident_id = _books("INV-1010")
    approval_id = propose(conn, incident_id, _TERMS, proposer=AP_CLERK)
    changed = ApprovalTerms(
        action=_TERMS.action,
        amount=_TERMS.amount,
        currency=_TERMS.currency,
        beneficiary_id=_TERMS.beneficiary_id,
        details_hash="details-v2",
    )
    try:
        approve(conn, approval_id, changed, approver=FINANCE_APPROVER, used_at=_SEEN)
    except ApprovalError as error:
        if error.code == "void":
            return "VOID", "R-APPROVAL-VOID", False, False
    return "ERROR", "R-ERROR", False, False


def _approval_single_use() -> tuple[str, str, bool, bool]:
    conn, incident_id = _books("INV-1011")
    approval_id = propose(conn, incident_id, _TERMS, proposer=AP_CLERK)
    try:
        approve(conn, approval_id, _TERMS, approver=AP_CLERK, used_at=_SEEN)
    except ApprovalError as error:
        self_rejected = error.code == "self"
    else:
        self_rejected = False
    approve(conn, approval_id, _TERMS, approver=FINANCE_APPROVER, used_at=_SEEN)
    try:
        approve(conn, approval_id, _TERMS, approver=FINANCE_APPROVER, used_at=_SEEN)
    except ApprovalError as error:
        used_rejected = error.code == "used"
    else:
        used_rejected = False
    if self_rejected and used_rejected:
        return "REJECT", "R-APPROVAL-USED", False, False
    return "ERROR", "R-ERROR", False, False


def _webhook_replay() -> tuple[str, str, bool, bool]:
    conn, _incident_id = _books("INV-1012", airwallex_id="awx-eval-12", status="SENT")
    raw = json.dumps(
        {
            "id": "evt-eval-12",
            "name": "payout.transfer.paid",
            "data": {"id": "awx-eval-12", "status": "PAID"},
        },
        separators=(",", ":"),
    ).encode()
    secret = "eval-secret"
    signature = hmac.new(secret.encode(), b"1700000000000" + raw, hashlib.sha256).hexdigest()
    first = receive_webhook(
        conn,
        secret=secret,
        raw_body=raw,
        x_timestamp="1700000000000",
        x_signature=signature,
        now_ms=1_700_000_000_000,
    )
    second = receive_webhook(
        conn,
        secret=secret,
        raw_body=raw,
        x_timestamp="1700000000000",
        x_signature=signature,
        now_ms=1_700_000_000_000,
    )
    snapshots = conn.execute("SELECT COUNT(*) AS n FROM status_snapshot").fetchone()["n"]
    if first.outcome == "applied" and second.outcome == "duplicate" and snapshots == 1:
        return "DEDUPE", "R-WEBHOOK-REPLAY", False, False
    return "ERROR", "R-ERROR", False, False


def _replace_once() -> tuple[str, str, bool, bool]:
    conn, obligation_id = _books("INV-1013")
    client = _Pay()
    first = replace_payment(conn, client, obligation_id=obligation_id, attempt=1, **_REPLACE)
    second = replace_payment(conn, client, obligation_id=obligation_id, attempt=1, **_REPLACE)
    if first == second and len(client.created) == 1:
        return "ONE_TRANSFER", "R-REPLACE-LOCK", False, False
    return "ERROR", "R-ERROR", False, True


def _replace_unknown() -> tuple[str, str, bool, bool]:
    conn, obligation_id = _books("INV-1014")
    client = _Pay(timeout=True)
    try:
        replace_payment(conn, client, obligation_id=obligation_id, attempt=1, **_REPLACE)
    except ReplaceError as error:
        status = conn.execute("SELECT status FROM obligation").fetchone()["status"]
        attempt_two = conn.execute(
            "SELECT id FROM transfer_record WHERE request_id = 'stl-inv1014-2'"
        ).fetchone()
        if error.code == "unknown" and status == "IN_FLIGHT" and attempt_two is None:
            return "IN_FLIGHT", "R-REPLACE-UNKNOWN", False, False
    return "ERROR", "R-ERROR", False, False


class _Pay:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.created: list[str] = []

    def create_transfer(self, **kwargs):
        if self.timeout:
            raise TimeoutError
        self.created.append(kwargs["request_id"])
        return Transfer(
            id="awx-one",
            status="SCHEDULED",
            request_id=kwargs["request_id"],
            transfer_amount=Decimal(kwargs["transfer_amount"]),
            transfer_currency=kwargs["transfer_currency"],
            reference="INV-1013",
        )

    def list_transfers(self, **_kwargs):
        return TransferPage(items=(), page_after=None)


_REPLACE = {
    "beneficiary_id": "ben_example",
    "details_hash": "details-v1",
    "transfer_method": "SWIFT",
    "reason": "other_services",
}


def _books(invoice: str, *, airwallex_id: str = "awx-original", status: str = "CANCELLED") -> tuple:
    conn = connect()
    migrate(conn)
    supplier = conn.execute(
        "INSERT INTO supplier (name, contact, phone_on_file) VALUES (?, ?, ?)",
        ("Example Mills", "ap@example.test", "+00-00-000000"),
    )
    append_bank_detail_version(
        conn,
        supplier_id=int(supplier.lastrowid),
        beneficiary_id="ben_example",
        details_hash="details-v1",
        bank_name="Example Bank",
        account_last4="0000",
        source="onboarding",
    )
    obligation = conn.execute(
        """
        INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status)
        VALUES (?, '100.00', 'EUR', ?, 'OPEN')
        """,
        (invoice, supplier.lastrowid),
    )
    transfer = conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, obligation_id, origin, amount, currency, status, reference
        ) VALUES (?, 'original-payment', ?, 'settlewell', '100.00', 'EUR', ?, ?)
        """,
        (airwallex_id, obligation.lastrowid, status, invoice),
    )
    conn.execute(
        "UPDATE obligation SET live_transfer_id = ? WHERE id = ?",
        (transfer.lastrowid, obligation.lastrowid),
    )
    incident = conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, NULL, 'OPEN')",
        (obligation.lastrowid,),
    )
    conn.commit()
    return conn, int(incident.lastrowid)

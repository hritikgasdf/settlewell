"""Bound, single-use approvals. A changed detail voids the grant."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

AP_CLERK = "ap-clerk"
FINANCE_APPROVER = "finance-approver"
DEMO_IDENTITIES = (AP_CLERK, FINANCE_APPROVER)


class ApprovalError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ApprovalTerms:
    action: str
    amount: str
    currency: str
    beneficiary_id: str
    details_hash: str
    quote_id: str | None = None
    evidence_hashes: tuple[str, ...] = ()


def binding_hash(terms: ApprovalTerms) -> str:
    payload = {
        "action": terms.action,
        "amount": format(Decimal(terms.amount), "f"),
        "currency": terms.currency,
        "beneficiary_id": terms.beneficiary_id,
        "details_hash": terms.details_hash,
        "quote_id": terms.quote_id or "",
        "evidence": sorted(terms.evidence_hashes),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def propose(
    conn,
    incident_id: int,
    terms: ApprovalTerms,
    *,
    proposer: str,
    expires_at: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO approval (incident_id, action, binding_hash, expires_at, proposer)
        VALUES (?, ?, ?, ?, ?)
        """,
        (incident_id, terms.action, binding_hash(terms), expires_at, proposer),
    )
    conn.commit()
    return int(cursor.lastrowid)


def approve(
    conn,
    approval_id: int,
    terms: ApprovalTerms,
    *,
    approver: str,
    used_at: str,
    before_update: Callable[[], None] | None = None,
) -> None:
    row = conn.execute("SELECT * FROM approval WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ApprovalError("missing")
    if row["used_at"] is not None:
        raise ApprovalError("used")
    if terms.action != row["action"] or binding_hash(terms) != row["binding_hash"]:
        raise ApprovalError("void")
    if approver == row["proposer"]:
        raise ApprovalError("self")
    if before_update is not None:
        before_update()
    cursor = conn.execute(
        "UPDATE approval SET used_at = ?, approver = ? WHERE id = ? AND used_at IS NULL",
        (used_at, approver, approval_id),
    )
    if cursor.rowcount == 0:
        raise ApprovalError("used")
    conn.commit()

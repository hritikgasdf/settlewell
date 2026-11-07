"""New supplier details become a new beneficiary only after validate and a call-back."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.approvals import AP_CLERK, ApprovalTerms, binding_hash, propose
from backend.execute import (
    CallbackConfirmation,
    ExecuteError,
    callback_ok,
    normalize_timestamp,
    open_intent,
)
from backend.ledger import append_audit, append_bank_detail_version

_IBAN_CODE = "083"


class BankChangeError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BankChangeCard:
    text: str
    details_hash: str
    beneficiary_id: str
    approval_id: int


def verify_bank_change(
    conn,
    client,
    *,
    obligation_id: int,
    incident_ref: str,
    bank_name: str,
    account_last4: str,
    beneficiary: Mapping,
    attested_by: str | None = None,
    attested_at: str | None = None,
    confirmation: CallbackConfirmation | None = None,
    proposer: str = AP_CLERK,
) -> BankChangeCard:
    """Validate the details and require a structured call-back. Creation waits for execute."""
    incident_id = _incident_id(incident_ref)
    incident = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if incident is None or obligation is None or incident["obligation_id"] != obligation_id:
        raise BankChangeError("incident")
    supplier = conn.execute(
        "SELECT * FROM supplier WHERE id = ?",
        (obligation["supplier_id"],),
    ).fetchone()
    body = _without_id(beneficiary)
    digest = beneficiary_details_hash(bank_name, account_last4, body)
    current = _verified(conn, obligation["supplier_id"], digest)
    if current is None:
        _remember_claim(conn, obligation["supplier_id"], digest, bank_name, account_last4)
        conn.commit()
        validation = client.validate_beneficiary(body)
        if not validation.ok:
            if _IBAN_CODE in validation.field_codes:
                raise BankChangeError("083")
            raise BankChangeError("invalid")
        phone = supplier["phone_on_file"] if supplier is not None else None
        try:
            callback_ok(confirmation, phone_on_file=phone, proposer=proposer)
        except ExecuteError as error:
            raise BankChangeError(error.code) from error
        text = _diff(conn, obligation["supplier_id"], 0, bank_name, account_last4)
        opened = datetime.fromisoformat(normalize_timestamp(confirmation.attested_at))
        intent = open_intent(
            conn,
            kind="beneficiary",
            incident_id=incident_id,
            obligation_id=obligation_id,
            action="NEW_BENEFICIARY",
            amount=obligation["amount"],
            currency=obligation["currency"],
            beneficiary_id="",
            details_hash=digest,
            proposer=proposer,
            body_json=json.dumps(
                {
                    "body": body,
                    "bank_name": bank_name,
                    "account_last4": account_last4,
                    "phone": confirmation.phone,
                    "confirmed": confirmation.confirmed,
                    "attested_by": confirmation.attested_by,
                    "attested_at": confirmation.attested_at,
                }
            ),
            expires_at=(opened + timedelta(hours=24)).isoformat(),
            created_at=confirmation.attested_at,
        )
        append_audit(
            conn,
            incident_id=incident_id,
            kind="bank_change",
            detail=text,
            created_at=confirmation.attested_at,
        )
        return BankChangeCard(text, digest, "", intent.approval_id)
    else:
        text = _diff(conn, obligation["supplier_id"], current["id"], bank_name, account_last4)
        beneficiary_id = current["beneficiary_id"]
    approval_id = _approval(
        conn,
        incident_id,
        obligation,
        beneficiary_id=beneficiary_id,
        details_hash=digest,
    )
    return BankChangeCard(text, digest, beneficiary_id, approval_id)


def _incident_id(incident_ref: str) -> int:
    try:
        return int(incident_ref)
    except (TypeError, ValueError):
        raise BankChangeError("incident") from None


def _without_id(beneficiary: Mapping) -> dict:
    return {key: value for key, value in beneficiary.items() if key != "id"}


def beneficiary_details_hash(bank_name: str, account_last4: str, body: Mapping) -> str:
    payload = {"account_last4": account_last4, "bank_name": bank_name, "beneficiary": body}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _verified(conn, supplier_id: int, digest: str):
    return conn.execute(
        """
        SELECT * FROM bank_detail_version
        WHERE supplier_id = ? AND details_hash = ? AND source = 'verified_callback'
          AND beneficiary_id IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (supplier_id, digest),
    ).fetchone()


def _remember_claim(conn, supplier_id: int, digest: str, bank_name: str, account_last4: str) -> None:
    existing = conn.execute(
        """
        SELECT id FROM bank_detail_version
        WHERE supplier_id = ? AND details_hash = ? AND source = 'email_claim'
        """,
        (supplier_id, digest),
    ).fetchone()
    if existing is not None:
        return
    append_bank_detail_version(
        conn,
        supplier_id=supplier_id,
        beneficiary_id=None,
        details_hash=digest,
        bank_name=bank_name,
        account_last4=account_last4,
        source="email_claim",
    )


def _attested(attested_by: str | None, attested_at: str | None, phone: str | None) -> bool:
    return bool(phone and phone.strip() and attested_by and attested_by.strip() and attested_at and attested_at.strip())


def _diff(conn, supplier_id: int, version_id: int, bank_name: str, account_last4: str) -> str:
    previous = conn.execute(
        """
        SELECT bank_name, account_last4 FROM bank_detail_version
        WHERE supplier_id = ? AND id != ?
          AND source IN ('onboarding', 'verified_callback')
        ORDER BY id DESC LIMIT 1
        """,
        (supplier_id, version_id),
    ).fetchone()
    new = _short(bank_name)
    if previous is None:
        return f"{new} ···{account_last4}"
    old = _short(previous["bank_name"])
    return f"{old} → {new} ···{previous['account_last4']} → ···{account_last4}"


def _short(bank_name: str) -> str:
    if bank_name.casefold() == "deutsche kreditbank":
        return "DKB"
    return bank_name


def _approval(conn, incident_id: int, obligation, *, beneficiary_id: str, details_hash: str) -> int:
    terms = ApprovalTerms(
        action="NEW_BENEFICIARY",
        amount=obligation["amount"],
        currency=obligation["currency"],
        beneficiary_id=beneficiary_id,
        details_hash=details_hash,
    )
    row = conn.execute(
        "SELECT id FROM approval WHERE incident_id = ? AND binding_hash = ?",
        (incident_id, binding_hash(terms)),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    return propose(conn, incident_id, terms, proposer=AP_CLERK)

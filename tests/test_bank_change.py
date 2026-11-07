"""New bank details stay unpaid until validate and a call-back. No network."""

from decimal import Decimal

import pytest

from backend.airwallex.types import (
    BalanceHistoryItem,
    BalanceHistoryPage,
    Beneficiary,
    BeneficiaryValidation,
    Transfer,
    TransferPage,
)
from backend.approvals import AP_CLERK, FINANCE_APPROVER, ApprovalTerms, approve, binding_hash
from backend.bank_change import BankChangeError, verify_bank_change
from backend.execute import CallbackConfirmation, execute, intent_terms, open_intent
from backend.ledger import connect, seed_hero
from backend.replace_payment import ReplaceError
from backend.replacement_tool import propose_replacement

NEW_BANK = {
    "id": "ben_old",
    "beneficiary": {
        "entity_type": "COMPANY",
        "bank_details": {
            "account_name": "Lumen Textiles GmbH",
            "account_currency": "EUR",
            "bank_country_code": "DE",
            "bank_name": "Deutsche Kreditbank",
            "iban": "DE89370400440532017781",
            "swift_code": "BYLADEM1001",
        },
    },
    "transfer_methods": ["SWIFT"],
}


class Desk:
    def __init__(self, *, field_codes: tuple[str, ...] = ()) -> None:
        self.field_codes = field_codes
        self.validated: list[dict] = []
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.paid: list[dict] = []

    def validate_beneficiary(self, body: dict) -> BeneficiaryValidation:
        self.validated.append(body)
        return BeneficiaryValidation(ok=not self.field_codes, field_codes=self.field_codes)

    def create_beneficiary(self, body: dict) -> Beneficiary:
        self.created.append(body)
        return Beneficiary(id="ben_dkb", nickname="Lumen DKB")

    def update_beneficiary(self, body: dict) -> Beneficiary:
        self.updated.append(body)
        raise AssertionError("a bank change creates a new beneficiary")

    def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer(
            id=transfer_id,
            status="CANCELLED",
            transfer_amount=Decimal("4200.00"),
            transfer_currency="EUR",
            transfer_method="SWIFT",
            fee_amount=Decimal("13.47"),
            failure_reason="90802: Beneficiary bank returned",
            reference="INV-2291",
        )

    def balance_history(self, **_kwargs) -> BalanceHistoryPage:
        return BalanceHistoryPage(
            items=(
                BalanceHistoryItem(
                    id="rev-1",
                    source="awx-hero",
                    amount=Decimal("4200.00"),
                    currency="EUR",
                    balance=None,
                    description="Refund",
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

    def create_transfer(self, **kwargs):
        self.paid.append(kwargs)
        return Transfer(
            id="awx-new",
            status="SCHEDULED",
            request_id=kwargs["request_id"],
            transfer_amount=kwargs["transfer_amount"],
            transfer_currency=kwargs["transfer_currency"],
            reference="INV-2291",
        )

    def list_transfers(self, **_kwargs):
        return TransferPage(items=(), page_after=None)


def _ready(conn):
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    conn.execute(
        "UPDATE transfer_record SET request_id = 'original-payment', status = 'CANCELLED'"
    )
    conn.commit()
    return obligation_id, str(incident_id)


def _change(conn, client, obligation_id, incident_ref, **overrides):
    fields = {
        "obligation_id": obligation_id,
        "incident_ref": incident_ref,
        "bank_name": "Deutsche Kreditbank",
        "account_last4": "7781",
        "beneficiary": NEW_BANK,
        "confirmation": CallbackConfirmation(
            phone="+49-30-000000",
            confirmed=True,
            attested_by="finance-approver",
            attested_at="2026-11-03T12:00:00+00:00",
        ),
    }
    fields.update(overrides)
    return verify_bank_change(conn, client, **fields)


def test_an_email_claim_cannot_be_paid_even_with_a_beneficiary_id():
    conn = connect()
    obligation_id, incident_ref = _ready(conn)
    supplier_id = conn.execute(
        "SELECT supplier_id FROM obligation WHERE id = ?",
        (obligation_id,),
    ).fetchone()["supplier_id"]
    conn.execute(
        """
        INSERT INTO bank_detail_version (
            supplier_id, beneficiary_id, details_hash, bank_name, account_last4, source
        ) VALUES (?, 'ben_email', 'claim-v1', 'Deutsche Kreditbank', '7781', 'email_claim')
        """,
        (supplier_id,),
    )
    conn.commit()
    client = Desk()

    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, client, obligation_id=obligation_id, incident_ref=incident_ref)

    assert caught.value.code == "unverified"
    assert client.paid == []


def test_iban_083_does_not_create_a_beneficiary_or_unlock_payment():
    conn = connect()
    obligation_id, incident_ref = _ready(conn)
    client = Desk(field_codes=("083",))

    with pytest.raises(BankChangeError) as caught:
        _change(conn, client, obligation_id, incident_ref)
    assert caught.value.code == "083"
    assert client.created == []
    assert client.updated == []
    sources = [
        row["source"]
        for row in conn.execute("SELECT source FROM bank_detail_version ORDER BY id")
    ]
    assert sources == ["onboarding", "email_claim"]

    with pytest.raises(ReplaceError) as paid:
        propose_replacement(conn, client, obligation_id=obligation_id, incident_ref=incident_ref)
    assert paid.value.code == "unverified"
    assert client.paid == []


def test_a_valid_iban_still_waits_for_the_call_back():
    conn = connect()
    obligation_id, incident_ref = _ready(conn)
    client = Desk()

    with pytest.raises(BankChangeError) as caught:
        _change(
            conn,
            client,
            obligation_id,
            incident_ref,
            attested_by=None,
            attested_at=None,
            confirmation=None,
        )

    assert caught.value.code == "callback"
    assert client.validated
    assert client.created == []
    assert client.updated == []


def test_verified_change_shows_commerzbank_to_dkb_and_binds_the_hash():
    conn = connect()
    obligation_id, incident_ref = _ready(conn)
    client = Desk()

    card = _change(conn, client, obligation_id, incident_ref)
    assert client.created == []
    intent_id = conn.execute(
        "SELECT id FROM execution_intent WHERE kind = 'beneficiary'"
    ).fetchone()["id"]
    approve(
        conn,
        card.approval_id,
        intent_terms(conn, intent_id),
        approver=FINANCE_APPROVER,
        used_at="2026-11-03T12:00:00+00:00",
    )
    created = execute(conn, client, intent_id, as_of="2026-11-03T12:00:00+00:00")
    again = _change(conn, client, obligation_id, incident_ref)

    assert card.text == "Commerzbank → DKB ···4400 → ···7781"
    assert again.text == card.text
    assert created.remote_id == "ben_dkb"
    assert again.beneficiary_id == "ben_dkb"
    sent = {key: value for key, value in NEW_BANK.items() if key != "id"}
    assert client.created == [sent]
    assert client.updated == []
    version = conn.execute(
        """
        SELECT beneficiary_id, details_hash, source, verified_by
        FROM bank_detail_version WHERE source = 'verified_callback'
        """
    ).fetchone()
    assert version["beneficiary_id"] == "ben_dkb"
    assert version["source"] == "verified_callback"
    assert version["verified_by"] == "finance-approver"
    assert version["details_hash"] == card.details_hash
    approval = conn.execute("SELECT action, binding_hash FROM approval").fetchone()
    assert approval["action"] == "NEW_BENEFICIARY"
    assert approval["binding_hash"] == binding_hash(
        ApprovalTerms(
            action="NEW_BENEFICIARY",
            amount="4200.00",
            currency="EUR",
            beneficiary_id="",
            details_hash=version["details_hash"],
            evidence_hashes=(f"intent:{intent_id}",),
        )
    )
    assert card.approval_id == conn.execute("SELECT id FROM approval").fetchone()["id"]
    pay_intent = open_intent(
        conn,
        kind="replacement",
        incident_id=int(incident_ref),
        obligation_id=obligation_id,
        action="REPLACE_SAME",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="ben_dkb",
        details_hash=version["details_hash"],
        proposer=AP_CLERK,
        failure_code="90802",
        created_at="2026-11-03T12:00:00+00:00",
    )
    approve(
        conn,
        pay_intent.approval_id,
        intent_terms(conn, pay_intent.id),
        approver=FINANCE_APPROVER,
        used_at="2026-11-03T12:10:00+00:00",
    )
    paid = propose_replacement(
        conn, client, obligation_id=obligation_id, incident_ref=incident_ref
    )
    assert paid == "awx-new"
    assert client.paid[0]["beneficiary_id"] == "ben_dkb"

"""The replacement tool accepts an obligation and an incident, never an amount."""

import inspect
from decimal import Decimal

import pytest

from backend.airwallex.types import Transfer, TransferPage
from backend.ledger import connect, seed_hero
from backend.replacement_tool import propose_replacement


class Record:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer(
            id=transfer_id,
            status="CANCELLED",
            transfer_amount=Decimal("4200.50"),
            transfer_currency="EUR",
            transfer_method="SWIFT",
            reference="INV-2291",
        )

    def create_transfer(self, **kwargs):
        self.created.append(kwargs)
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


def test_the_tool_signature_has_no_amount():
    params = inspect.signature(propose_replacement).parameters
    assert set(params) == {"conn", "client", "obligation_id", "incident_ref"}
    with pytest.raises(TypeError):
        propose_replacement(object(), object(), obligation_id=1, incident_ref="1", amount="1")


def test_amount_and_beneficiary_come_from_the_record_not_the_caller():
    conn = connect()
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    conn.execute(
        "UPDATE transfer_record SET request_id = 'original-payment', status = 'CANCELLED'"
    )
    supplier_id = conn.execute(
        "SELECT supplier_id FROM obligation WHERE id = ?",
        (obligation_id,),
    ).fetchone()["supplier_id"]
    conn.execute(
        """
        INSERT INTO bank_detail_version (
            supplier_id, beneficiary_id, details_hash, bank_name, account_last4, source,
            verified_by, verified_at
        ) VALUES (?, 'ben_verified', 'callback-v2', 'Deutsche Kreditbank', '7781',
                  'verified_callback', 'finance', '2026-11-03T12:00:00+00:00')
        """,
        (supplier_id,),
    )
    conn.commit()
    client = Record()
    airwallex_id = propose_replacement(
        conn,
        client,
        obligation_id=obligation_id,
        incident_ref=str(incident_id),
    )
    assert airwallex_id == "awx-new"
    sent = client.created[0]
    assert sent["transfer_amount"] == Decimal("4200.50")
    assert sent["transfer_currency"] == "EUR"
    assert sent["beneficiary_id"] == "ben_verified"
    local = conn.execute("SELECT amount FROM obligation").fetchone()["amount"]
    assert local == "4200.00"
    assert sent["transfer_amount"] != Decimal(local)

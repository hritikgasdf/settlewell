"""Money moves only through the execution service. No network."""

import sqlite3
from decimal import Decimal

import pytest

from backend.approvals import AP_CLERK, FINANCE_APPROVER, approve
from backend.bank_change import BankChangeError, verify_bank_change
from backend.execute import (
    CallbackConfirmation,
    ExecuteError,
    ExecuteResult,
    execute,
    intent_terms,
    open_intent,
)
from backend.fx_funding import fund_replacement
from backend.ledger import connect, migrate, seed_hero
from backend.replace_payment import ReplaceError, replace_payment
from backend.replacement_tool import propose_replacement

SEEN = "2026-11-03T12:00:00+00:00"
EXPIRES = "2026-11-03T13:00:00+00:00"


class Pay:
    def __init__(
        self,
        *,
        reason: str = "90701: Account closed",
        amount: str = "4200.00",
        eur_available: str = "100000.00",
    ) -> None:
        self.reason = reason
        self.amount = amount
        self.eur_available = eur_available
        self.created: list[dict] = []
        self.conversions: list[dict] = []
        self.beneficiaries: list[dict] = []

    def get_transfer(self, transfer_id: str):
        from backend.airwallex.types import Transfer

        return Transfer(
            id=transfer_id,
            status="CANCELLED",
            request_id="original-payment",
            transfer_amount=Decimal(self.amount),
            transfer_currency="EUR",
            transfer_method="SWIFT",
            fee_amount=Decimal("13.47"),
            fee_currency="EUR",
            failure_reason=self.reason,
            reference="INV-2291",
        )

    def balance_history(self, **_kwargs):
        from backend.airwallex.types import BalanceHistoryItem, BalanceHistoryPage

        return BalanceHistoryPage(
            items=(
                BalanceHistoryItem(
                    id="rev",
                    source="awx-hero",
                    amount=Decimal(self.amount),
                    currency="EUR",
                    balance=None,
                    description=None,
                    fee=Decimal(0),
                    posted_at=None,
                    source_type="PAYOUT",
                    transaction_type="PAYOUT_REVERSAL",
                    account_type="CASH",
                    reference="INV-2291",
                ),
            ),
            has_more=False,
            page_after=None,
        )

    def list_transfers(self, **_kwargs):
        from backend.airwallex.types import TransferPage

        return TransferPage(items=(), page_after=None)

    def create_transfer(self, **kwargs):
        self.created.append(kwargs)
        from backend.airwallex.types import Transfer

        return Transfer(
            id="awx-new",
            status="SCHEDULED",
            request_id=kwargs["request_id"],
            transfer_amount=kwargs["transfer_amount"],
            transfer_currency=kwargs["transfer_currency"],
            fee_amount=Decimal("13.47"),
            fee_currency="EUR",
            reference="INV-2291",
        )

    def validate_beneficiary(self, _body):
        from backend.airwallex.types import BeneficiaryValidation

        return BeneficiaryValidation(ok=True)

    def create_beneficiary(self, body):
        self.beneficiaries.append(body)
        from backend.airwallex.types import Beneficiary

        return Beneficiary(id="ben_dkb")

    def create_conversion(self, **kwargs):
        self.conversions.append(kwargs)
        from backend.airwallex.types import Conversion

        return Conversion(conversion_id="con_exec", status="SETTLED", quote_id=kwargs["quote_id"])

    def create_quote(self, **kwargs):
        from backend.airwallex.types import FxQuote

        return FxQuote(
            quote_id="quo_exec",
            client_rate=Decimal("1.10"),
            buy_currency="EUR",
            sell_currency="USD",
            buy_amount=kwargs["buy_amount"],
            sell_amount=kwargs["buy_amount"],
            valid_to_at="2026-11-03T12:15:00+00:00",
            usage="MULTI_USE",
        )

    def current_balances(self):
        from backend.airwallex.types import Balance

        def row(currency: str, amount: str) -> Balance:
            value = Decimal(amount)
            return Balance(currency, value, Decimal(0), Decimal(0), value, Decimal(0), account_type="cash")

        return [row("EUR", self.eur_available), row("USD", "100000.00")]


def _ready(conn, *, reason: str = "90701: Account closed"):
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    conn.execute(
        """
        UPDATE transfer_record
        SET request_id = 'original-payment', status = 'CANCELLED', failure_message = ?, failure_code = ?
        WHERE airwallex_id = 'awx-hero'
        """,
        (reason, reason.split(":", 1)[0]),
    )
    conn.execute(
        """
        INSERT INTO status_snapshot (transfer_record_id, status, source, seen_at)
        SELECT id, 'CANCELLED', 'poll', ? FROM transfer_record WHERE airwallex_id = 'awx-hero'
        """,
        (SEEN,),
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
                  'verified_callback', 'finance-approver', ?)
        """,
        (supplier_id, SEEN),
    )
    conn.commit()
    return obligation_id, incident_id


def _grant(
    conn,
    incident_id,
    *,
    failure_code: str = "90701",
    expires_at: str | None = EXPIRES,
    quote_id: str | None = None,
    amount: str = "4200.00",
    beneficiary_id: str = "ben_verified",
):
    ref = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=conn.execute(
            "SELECT obligation_id FROM incident WHERE id = ?",
            (incident_id,),
        ).fetchone()["obligation_id"],
        action="REPLACE_NEW_DETAILS",
        amount=amount,
        currency="EUR",
        beneficiary_id=beneficiary_id,
        details_hash="callback-v2",
        proposer=AP_CLERK,
        failure_code=failure_code,
        quote_id=quote_id,
        expires_at=expires_at,
        created_at=SEEN,
    )
    approve(
        conn,
        ref.approval_id,
        intent_terms(conn, ref.id),
        approver=FINANCE_APPROVER,
        used_at=SEEN,
    )
    return ref


def test_a_replacement_without_approval_does_not_pay():
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    client = Pay()

    ref = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="REPLACE_NEW_DETAILS",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="ben_verified",
        details_hash="callback-v2",
        proposer=AP_CLERK,
        failure_code="90701",
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(
            conn,
            client,
            obligation_id=obligation_id,
            incident_ref=str(incident_id),
            intent_id=ref.id,
            as_of=SEEN,
        )

    assert caught.value.code == "unapproved"
    assert client.created == []


def test_a_recall_never_pays_even_with_approval():
    conn = connect()
    _obligation_id, incident_id = _ready(conn, reason="91001: Recall requested")
    client = Pay(reason="91001: Recall requested")
    ref = _grant(conn, incident_id, failure_code="91001")

    with pytest.raises(ExecuteError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)

    assert caught.value.code == "policy"
    assert client.created == []


def test_an_approved_replacement_pays_once():
    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    client = Pay()
    ref = _grant(conn, incident_id)

    result = execute(conn, client, ref.id, as_of=SEEN)

    assert isinstance(result, ExecuteResult)
    assert result.remote_id == "awx-new"
    assert len(client.created) == 1
    again = execute(conn, client, ref.id, as_of=SEEN)
    assert again.remote_id == "awx-new"
    assert len(client.created) == 1


def test_an_expired_grant_does_not_pay():
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    client = Pay()
    ref = _grant(conn, incident_id, expires_at="2026-11-03T11:00:00+00:00")

    with pytest.raises(ReplaceError) as caught:
        propose_replacement(
            conn,
            client,
            obligation_id=obligation_id,
            incident_ref=str(incident_id),
            intent_id=ref.id,
            as_of=SEEN,
        )

    assert caught.value.code == "expired"
    assert client.created == []


def test_fx_conversion_waits_for_approval_of_the_locked_quote():
    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    client = Pay(eur_available="0.00")
    locked = fund_replacement(conn, client, obligation_id=_obligation_id, as_of=SEEN)
    assert locked.action == "locked"
    assert client.conversions == []
    ref = open_intent(
        conn,
        kind="fx_conversion",
        incident_id=incident_id,
        obligation_id=_obligation_id,
        action="FX_FUND",
        amount=format(locked.buy_amount, "f"),
        currency="EUR",
        beneficiary_id="ben_verified",
        details_hash="callback-v2",
        proposer=AP_CLERK,
        quote_id=locked.quote_id,
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    with pytest.raises(ExecuteError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)
    assert caught.value.code == "unapproved"
    assert client.conversions == []
    approve(
        conn,
        ref.approval_id,
        intent_terms(conn, ref.id),
        approver=FINANCE_APPROVER,
        used_at=SEEN,
    )
    result = execute(conn, client, ref.id, as_of=SEEN)
    assert result.status == "converted"
    assert len(client.conversions) == 1
    again = execute(conn, client, ref.id, as_of=SEEN)
    assert again.remote_id == result.remote_id
    assert len(client.conversions) == 1


def test_free_text_is_not_a_call_back_and_create_waits_for_approval():
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    client = Pay()
    body = {"beneficiary": {"bank_details": {"bank_name": "Deutsche Kreditbank", "iban": "DE02120300000000202051"}}}

    with pytest.raises(BankChangeError) as caught:
        verify_bank_change(
            conn,
            client,
            obligation_id=obligation_id,
            incident_ref=str(incident_id),
            bank_name="Deutsche Kreditbank",
            account_last4="2051",
            beneficiary=body,
            attested_by="called them",
            attested_at=SEEN,
        )
    assert caught.value.code == "callback"
    assert client.beneficiaries == []

    confirmation = CallbackConfirmation(
        phone="+49-30-000000",
        confirmed=True,
        attested_by=FINANCE_APPROVER,
        attested_at=SEEN,
    )
    card = verify_bank_change(
        conn,
        client,
        obligation_id=obligation_id,
        incident_ref=str(incident_id),
        bank_name="Deutsche Kreditbank",
        account_last4="2051",
        beneficiary=body,
        confirmation=confirmation,
    )
    assert client.beneficiaries == []
    assert "DKB" in card.text
    with pytest.raises(ExecuteError) as unapproved:
        execute(conn, client, _intent_id(conn), as_of=SEEN)
    assert unapproved.value.code == "unapproved"
    approve(
        conn,
        card.approval_id,
        intent_terms(conn, _intent_id(conn)),
        approver=FINANCE_APPROVER,
        used_at=SEEN,
    )
    created = execute(conn, client, _intent_id(conn), as_of=SEEN)
    assert created.remote_id == "ben_dkb"
    assert len(client.beneficiaries) == 1


def _observe_cancelled(conn) -> None:
    """The original transfer was observed CANCELLED at the fixture clock."""
    conn.execute(
        """
        INSERT INTO status_snapshot (transfer_record_id, status, source, seen_at)
        SELECT id, 'CANCELLED', 'poll', ? FROM transfer_record WHERE airwallex_id = 'awx-hero'
        """,
        (SEEN,),
    )


def _intent_id(conn) -> int:
    return int(conn.execute("SELECT id FROM execution_intent ORDER BY id DESC LIMIT 1").fetchone()["id"])


def test_same_details_replacement_uses_the_onboarding_beneficiary():
    conn = connect()
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    conn.execute(
        """
        UPDATE transfer_record
        SET status = 'CANCELLED', failure_code = '90802'
        WHERE airwallex_id = 'awx-hero'
        """
    )
    _observe_cancelled(conn)
    conn.execute(
        "UPDATE bank_detail_version SET beneficiary_id = 'ben_onboarding' WHERE source = 'onboarding'"
    )
    conn.commit()
    ref = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="REPLACE_SAME",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="ben_onboarding",
        details_hash="onboarding-v1",
        proposer=AP_CLERK,
        failure_code="90802",
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(conn, ref.approval_id, intent_terms(conn, ref.id), approver=FINANCE_APPROVER, used_at=SEEN)
    client = Pay(reason="90802: Beneficiary bank returned")
    client.get_transfer = _with_beneficiary(client.get_transfer, "ben_onboarding")

    result = execute(conn, client, ref.id, as_of=SEEN)

    assert result.remote_id == "awx-new"
    assert client.created[0]["beneficiary_id"] == "ben_onboarding"
    assert conn.execute(
        "SELECT id FROM bank_detail_version WHERE source = 'verified_callback'"
    ).fetchone() is None


def test_a_crash_mid_payout_is_recovered_by_request_id():
    conn = connect()
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    conn.execute(
        "UPDATE transfer_record SET status = 'CANCELLED', failure_code = '90802' WHERE airwallex_id = 'awx-hero'"
    )
    _observe_cancelled(conn)
    conn.execute(
        "UPDATE bank_detail_version SET beneficiary_id = 'ben_onboarding' WHERE source = 'onboarding'"
    )
    conn.commit()
    ref = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="REPLACE_SAME",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="ben_onboarding",
        details_hash="onboarding-v1",
        proposer=AP_CLERK,
        failure_code="90802",
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(conn, ref.approval_id, intent_terms(conn, ref.id), approver=FINANCE_APPROVER, used_at=SEEN)
    client = Pay(reason="90802: Beneficiary bank returned")
    client.get_transfer = _with_beneficiary(client.get_transfer, "ben_onboarding")
    client.accepted = None

    def create_transfer(**kwargs):
        from backend.airwallex.types import Transfer

        client.created.append(kwargs)
        client.accepted = Transfer(
            id="awx-crash",
            status="SCHEDULED",
            request_id=kwargs["request_id"],
            transfer_amount=kwargs["transfer_amount"],
            transfer_currency=kwargs["transfer_currency"],
            beneficiary_id=kwargs["beneficiary_id"],
            reference="INV-2291",
        )
        raise RuntimeError("crash")

    def list_transfers(**kwargs):
        from backend.airwallex.types import TransferPage

        if client.accepted is not None and kwargs.get("request_id") == client.accepted.request_id:
            return TransferPage(items=(client.accepted,), page_after=None)
        return TransferPage(items=(), page_after=None)

    client.create_transfer = create_transfer
    client.list_transfers = list_transfers

    with pytest.raises(RuntimeError, match="crash"):
        execute(conn, client, ref.id, as_of=SEEN)

    pending = conn.execute(
        "SELECT airwallex_id, request_id, status FROM transfer_record WHERE request_id = 'stl-inv2291-1'"
    ).fetchone()
    assert pending["airwallex_id"] is None
    assert pending["status"] == "IN_FLIGHT"
    stored = conn.execute("SELECT request_id, remote_id FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
    assert stored["request_id"] == "stl-inv2291-1"
    assert stored["remote_id"] is None

    recovered = execute(conn, client, ref.id, as_of=SEEN)

    assert recovered.remote_id == "awx-crash"
    assert len(client.created) == 1
    attached = conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE request_id = 'stl-inv2291-1'"
    ).fetchone()
    assert attached["airwallex_id"] == "awx-crash"


def test_restart_recovers_our_accepted_payout_before_the_colleague_scan(tmp_path):
    from backend.airwallex.types import Transfer, TransferPage

    path = tmp_path / "restart.db"
    conn = connect(path)
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    conn.execute(
        "UPDATE transfer_record SET status = 'CANCELLED', failure_code = '90802' WHERE airwallex_id = 'awx-hero'"
    )
    _observe_cancelled(conn)
    conn.execute(
        "UPDATE bank_detail_version SET beneficiary_id = 'ben_onboarding' WHERE source = 'onboarding'"
    )
    conn.commit()
    ref = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="REPLACE_SAME",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="ben_onboarding",
        details_hash="onboarding-v1",
        proposer=AP_CLERK,
        failure_code="90802",
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(conn, ref.approval_id, intent_terms(conn, ref.id), approver=FINANCE_APPROVER, used_at=SEEN)
    client = Pay(reason="90802: Beneficiary bank returned")
    client.get_transfer = _with_beneficiary(client.get_transfer, "ben_onboarding")
    accepted = []

    def create_transfer(**kwargs):
        client.created.append(kwargs)
        accepted.append(
            Transfer(
                id="awx-crash",
                status="SCHEDULED",
                request_id=kwargs["request_id"],
                transfer_amount=kwargs["transfer_amount"],
                transfer_currency=kwargs["transfer_currency"],
                beneficiary_id=kwargs["beneficiary_id"],
                reference="INV-2291",
            )
        )
        raise RuntimeError("crash")

    def list_transfers(**kwargs):
        wanted = kwargs.get("request_id")
        items = tuple(item for item in accepted if wanted is None or item.request_id == wanted)
        return TransferPage(items=items, page_after=None)

    client.create_transfer = create_transfer
    client.list_transfers = list_transfers

    with pytest.raises(RuntimeError, match="crash"):
        execute(conn, client, ref.id, as_of=SEEN)
    conn.close()

    conn = connect(path)
    assert len(client.list_transfers(page_size=50, page=None).items) == 1

    recovered = execute(conn, client, ref.id, as_of=SEEN)

    assert recovered.remote_id == "awx-crash"
    assert len(client.created) == 1
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_id,)).fetchone()["state"] != "CLOSED"
    assert conn.execute("SELECT id FROM audit_event WHERE kind = 'colleague_repaid'").fetchone() is None
    rows = conn.execute(
        "SELECT origin, airwallex_id FROM transfer_record WHERE airwallex_id = 'awx-crash'"
    ).fetchall()
    assert [(row["origin"], row["airwallex_id"]) for row in rows] == [("settlewell", "awx-crash")]
    conn.close()


def _with_beneficiary(getter, beneficiary_id: str):
    def get_transfer(transfer_id: str):
        remote = getter(transfer_id)
        return type(remote)(
            **{
                **remote.__dict__,
                "beneficiary_id": beneficiary_id,
            }
        )

    return get_transfer


def test_a_finite_expiry_still_pays_before_it_lapses():
    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    client = Pay()
    ref = _grant(conn, incident_id, expires_at="2026-11-03T13:00:00Z")

    result = execute(conn, client, ref.id, as_of="2026-11-03T12:30:00+00:00")

    assert result.remote_id == "awx-new"
    assert len(client.created) == 1


def test_an_open_intent_without_an_expiry_is_refused():
    conn = connect()
    _obligation_id, incident_id = _ready(conn)

    with pytest.raises(ExecuteError) as caught:
        open_intent(
            conn,
            kind="replacement",
            incident_id=incident_id,
            obligation_id=_obligation_id,
            action="REPLACE_NEW_DETAILS",
            amount="4200.00",
            currency="EUR",
            beneficiary_id="ben_verified",
            details_hash="callback-v2",
            proposer=AP_CLERK,
            failure_code="90701",
            expires_at=None,
            created_at=SEEN,
        )
    assert caught.value.code == "expiry"


def test_another_invoices_approval_does_not_pay():
    conn = connect()
    obligation_id, _incident_id = _ready(conn)
    other = conn.execute(
        """
        INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status, live_transfer_id)
        SELECT 'INV-OTHER', amount, currency, supplier_id, status, live_transfer_id
        FROM obligation WHERE id = ?
        """,
        (obligation_id,),
    )
    conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, NULL, 'OPEN')",
        (other.lastrowid,),
    )
    conn.commit()
    other_incident = conn.execute(
        "SELECT id FROM incident WHERE obligation_id = ?",
        (other.lastrowid,),
    ).fetchone()["id"]
    theirs = open_intent(
        conn,
        kind="replacement",
        incident_id=other_incident,
        obligation_id=int(other.lastrowid),
        action="REPLACE_NEW_DETAILS",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="ben_verified",
        details_hash="callback-v2",
        proposer=AP_CLERK,
        failure_code="90701",
        quote_id="quo_same",
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(
        conn,
        theirs.approval_id,
        intent_terms(conn, theirs.id),
        approver=FINANCE_APPROVER,
        used_at=SEEN,
    )
    client = Pay()

    with pytest.raises(ReplaceError) as caught:
        replace_payment(
            conn,
            client,
            obligation_id=obligation_id,
            attempt=1,
            beneficiary_id="ben_verified",
            details_hash="callback-v2",
            transfer_method="SWIFT",
            reason="other_services",
            as_of=SEEN,
            intent_id=theirs.id,
            approval_id=theirs.approval_id,
            quote_id="quo_same",
        )

    assert caught.value.code == "obligation"
    assert client.created == []
    state = conn.execute("SELECT state FROM execution_intent WHERE id = ?", (theirs.id,)).fetchone()
    assert state["state"] == "proposed"


def test_replaying_one_of_two_identical_intents_pays_once():
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    client = Pay()
    first = _grant(conn, incident_id)  # no FX top-up: a quote id would need a SETTLED conversion
    second = _grant(conn, incident_id)

    paid = execute(conn, client, first.id, as_of=SEEN)
    again = execute(conn, client, first.id, as_of=SEEN)

    assert paid.remote_id == "awx-new"
    assert again.remote_id == "awx-new"
    assert len(client.created) == 1
    with pytest.raises(ReplaceError) as caught:
        replace_payment(
            conn,
            client,
            obligation_id=obligation_id,
            attempt=1,
            beneficiary_id="ben_verified",
            details_hash="callback-v2",
            transfer_method="SWIFT",
            reason="other_services",
            as_of=SEEN,
            intent_id=second.id,
            approval_id=second.approval_id,
        )
    assert caught.value.code == "live"
    assert len(client.created) == 1
    stored = conn.execute("SELECT state FROM execution_intent WHERE id = ?", (second.id,)).fetchone()
    assert stored["state"] == "proposed"


def test_a_different_quote_does_not_spend_the_replacement_grant():
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    client = Pay()
    ref = _grant(conn, incident_id, quote_id="quo_locked")

    with pytest.raises(ReplaceError) as caught:
        replace_payment(
            conn,
            client,
            obligation_id=obligation_id,
            attempt=1,
            beneficiary_id="ben_verified",
            details_hash="callback-v2",
            transfer_method="SWIFT",
            reason="other_services",
            as_of=SEEN,
            intent_id=ref.id,
            approval_id=ref.approval_id,
            quote_id="quo_other",
        )

    assert caught.value.code == "void"
    assert client.created == []
    # The right quote now reaches the funding gate, which refuses: no SETTLED conversion exists.
    with pytest.raises(ReplaceError) as refused:
        replace_payment(
            conn,
            client,
            obligation_id=obligation_id,
            attempt=1,
            beneficiary_id="ben_verified",
            details_hash="callback-v2",
            transfer_method="SWIFT",
            reason="other_services",
            as_of=SEEN,
            intent_id=ref.id,
            approval_id=ref.approval_id,
            quote_id="quo_locked",
        )
    assert refused.value.code == "conversion"
    assert client.created == []


def test_a_changed_amount_at_execution_does_not_pay():
    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    client = Pay(amount="4300.00")
    ref = _grant(conn, incident_id)

    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)

    assert caught.value.code == "void"
    assert client.created == []


def test_a_changed_beneficiary_at_execution_does_not_pay():
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    ref = _grant(conn, incident_id)
    supplier_id = conn.execute(
        "SELECT supplier_id FROM obligation WHERE id = ?",
        (obligation_id,),
    ).fetchone()["supplier_id"]
    conn.execute(
        """
        INSERT INTO bank_detail_version (
            supplier_id, beneficiary_id, details_hash, bank_name, account_last4, source,
            verified_by, verified_at
        ) VALUES (?, 'ben_other', 'callback-v3', 'Other Bank', '9999',
                  'verified_callback', 'finance-approver', ?)
        """,
        (supplier_id, SEEN),
    )
    conn.commit()
    client = Pay()

    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)

    assert caught.value.code == "void"
    assert client.created == []


def test_two_threads_execute_one_replacement(tmp_path):
    import threading

    path = tmp_path / "execute.db"
    setup = connect(path)
    migrate(setup)
    _obligation_id, incident_id = _ready(setup)
    ref = _grant(setup, incident_id)
    setup.close()
    client = Pay()
    barrier = threading.Barrier(2)
    found: list[str] = []

    def worker() -> None:
        local = connect(path)
        try:
            barrier.wait()
            found.append(execute(local, client, ref.id, as_of=SEEN).remote_id)
        finally:
            local.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert found == ["awx-new", "awx-new"]
    assert len(client.created) == 1


def test_a_worker_that_read_the_intent_before_the_other_recorded_recovers_attempt_one(
    tmp_path, monkeypatch
):
    """Worker B starts before A records; A finishes before B reads the stored request id."""
    import backend.replacement_tool as tool

    path = tmp_path / "interleave.db"
    setup = connect(path)
    migrate(setup)
    _obligation_id, incident_id = _ready(setup)
    ref = _grant(setup, incident_id)
    setup.close()
    client = Pay()
    first = connect(path)
    second = connect(path)
    original = tool._attempt_for
    ran = []
    started = []

    def run_other_worker_first(*args, **kwargs):
        if not started:
            started.append(True)
            ran.append(execute(first, client, ref.id, as_of=SEEN).remote_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(tool, "_attempt_for", run_other_worker_first)
    try:
        # Worker B has read the intent (no request id yet); worker A then records attempt 1.
        result = execute(second, client, ref.id, as_of=SEEN).remote_id
    finally:
        first.close()
        second.close()

    assert ran == ["awx-new"]
    assert result == "awx-new"
    assert len(client.created) == 1


def test_a_raw_beneficiary_intent_cannot_bypass_the_call_back():
    conn = connect()
    obligation_id, incident_id = _ready(conn)
    ref = open_intent(
        conn,
        kind="beneficiary",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="NEW_BENEFICIARY",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="",
        details_hash="not-a-callback",
        proposer=AP_CLERK,
        body_json='{"note": "called them"}',
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(
        conn,
        ref.approval_id,
        intent_terms(conn, ref.id),
        approver=FINANCE_APPROVER,
        used_at=SEEN,
    )
    client = Pay()

    with pytest.raises(ExecuteError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)

    assert caught.value.code == "callback"
    assert client.beneficiaries == []


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


def test_beneficiary_create_recovers_after_the_local_write_fails():
    from backend.airwallex.types import Beneficiary

    conn = connect()
    obligation_id, incident_id = _ready(conn)
    client_setup = Pay()
    body = {"beneficiary": {"bank_details": {"bank_name": "Deutsche Kreditbank"}}}
    confirmation = CallbackConfirmation(
        phone="+49-30-000000",
        confirmed=True,
        attested_by=FINANCE_APPROVER,
        attested_at=SEEN,
    )
    card = verify_bank_change(
        conn,
        client_setup,
        obligation_id=obligation_id,
        incident_ref=str(incident_id),
        bank_name="Deutsche Kreditbank",
        account_last4="2051",
        beneficiary=body,
        confirmation=confirmation,
    )
    intent = _intent_id(conn)
    approve(conn, card.approval_id, intent_terms(conn, intent), approver=FINANCE_APPROVER, used_at=SEEN)

    class Desk:
        def __init__(self) -> None:
            self.created: list[dict] = []
            self.saved: dict[str, str] = {}

        def create_beneficiary(self, payload):
            self.created.append(payload)
            self.saved[payload["request_id"]] = "ben_crash"
            return Beneficiary(id="ben_crash")

        def find_beneficiary(self, request_id):
            if request_id not in self.saved:
                return None
            return Beneficiary(id=self.saved[request_id])

    desk = Desk()
    proxy = _FailingCommit(conn, fail_on=2)
    with pytest.raises(sqlite3.OperationalError):
        execute(proxy, desk, intent, as_of=SEEN)
    proxy.rollback()
    proxy.armed = False

    recovered = execute(proxy, desk, intent, as_of=SEEN)

    assert recovered.remote_id == "ben_crash"
    assert len(desk.created) == 1


def test_a_beneficiary_whose_hash_does_not_match_its_payload_is_refused():
    import json

    conn = connect()
    obligation_id, incident_id = _ready(conn)
    payload = {
        "body": {"beneficiary": {"bank_details": {"iban": "DE02120300000000202051"}}},
        "bank_name": "Deutsche Kreditbank",
        "account_last4": "2051",
        "phone": "+49-30-000000",
        "confirmed": True,
        "attested_by": FINANCE_APPROVER,
        "attested_at": SEEN,
    }
    ref = open_intent(
        conn,
        kind="beneficiary",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="NEW_BENEFICIARY",
        amount="4200.00",
        currency="EUR",
        beneficiary_id="",
        details_hash="not-the-payload",
        proposer=AP_CLERK,
        body_json=json.dumps(payload),
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(conn, ref.approval_id, intent_terms(conn, ref.id), approver=FINANCE_APPROVER, used_at=SEEN)
    client = Pay()

    with pytest.raises(ExecuteError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)

    assert caught.value.code == "void"
    assert client.beneficiaries == []


def _scan_client(pages: dict, first: str):
    """Pay whose account listing serves pages keyed by token. Recovery reads by request_id stay empty."""
    from backend.airwallex.types import TransferPage

    client = Pay()

    def list_transfers(**kwargs):
        if kwargs.get("request_id"):
            return TransferPage(items=(), page_after=None)
        return pages[kwargs.get("page") or first]

    original_get = client.get_transfer

    def get_transfer(transfer_id):
        for page in pages.values():
            for item in page.items:
                if item.id == transfer_id:
                    return item
        return original_get(transfer_id)

    client.list_transfers = list_transfers
    client.get_transfer = get_transfer
    return client


def _extra(transfer_id: str, status: str, **overrides):
    from backend.airwallex.types import Transfer

    fields = {
        "id": transfer_id,
        "status": status,
        "request_id": "hand-" + transfer_id,
        "transfer_amount": Decimal("4200.00"),
        "transfer_currency": "EUR",
        "reference": "INV-2291",
        "beneficiary_id": "ben_verified",
    }
    fields.update(overrides)
    return Transfer(**fields)


def _refused_untouched(conn, client, ref, incident_id, *, expect_state: str):
    before = tuple(conn.execute("SELECT * FROM execution_intent WHERE id = ?", (ref.id,)).fetchone())
    with pytest.raises(ReplaceError):
        execute(conn, client, ref.id, as_of=SEEN)
    assert client.created == []
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_id,)).fetchone()["state"] == "OPEN"
    assert conn.execute("SELECT id FROM incident WHERE state = ?", (expect_state,)).fetchone() is not None
    # The grant's execution fields (state, request id, remote result) are unchanged.
    assert tuple(conn.execute("SELECT * FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()) == before
    assert before[[c[0] for c in conn.execute("SELECT * FROM execution_intent").description].index("state")] == "proposed"
    assert conn.execute("SELECT id FROM transfer_record WHERE origin = 'settlewell' AND airwallex_id = 'awx-new'").fetchone() is None


def test_a_duplicate_after_page_five_blocks_payout():
    from backend.airwallex.types import TransferPage

    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    filler = tuple(_extra("f" + str(i), "PAID", reference="INV-" + str(i)) for i in range(50))
    pages = {str(n): TransferPage(items=filler, page_after=str(n + 1)) for n in range(1, 6)}
    pages["start"] = TransferPage(items=filler, page_after="1")
    pages["6"] = TransferPage(items=(_extra("awx-late", "SCHEDULED"),), page_after=None)
    client = _scan_client(pages, "start")
    ref = _grant(conn, incident_id)

    _refused_untouched(conn, client, ref, incident_id, expect_state="DUPLICATE")


def test_a_paid_known_supplier_payment_after_page_five_closes_and_never_sends():
    from backend.airwallex.types import TransferPage

    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    filler = tuple(_extra("f" + str(i), "PAID", reference="INV-" + str(i)) for i in range(50))
    pages = {str(n): TransferPage(items=filler, page_after=str(n + 1)) for n in range(1, 6)}
    pages["start"] = TransferPage(items=filler, page_after="1")
    pages["6"] = TransferPage(items=(_extra("awx-late", "PAID"),), page_after=None)
    client = _scan_client(pages, "start")
    ref = _grant(conn, incident_id)

    with pytest.raises(ReplaceError):
        execute(conn, client, ref.id, as_of=SEEN)

    assert client.created == []
    assert conn.execute("SELECT state FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()["state"] == "proposed"
    assert conn.execute("SELECT detail FROM audit_event WHERE kind = 'colleague_repaid'").fetchone()["detail"] == "awx-late"


def test_an_ambiguous_supplier_scan_opens_its_own_incident():
    from backend.airwallex.types import TransferPage

    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    stray = _extra("awx-stray", "PAID", beneficiary_id="ben_unknown")
    client = _scan_client({"start": TransferPage(items=(stray,), page_after=None)}, "start")
    ref = _grant(conn, incident_id)
    assert conn.execute("SELECT id FROM incident WHERE state = 'AMBIGUOUS'").fetchone() is None

    _refused_untouched(conn, client, ref, incident_id, expect_state="AMBIGUOUS")


def test_a_scheduled_colleague_payment_opens_its_own_incident_and_leaves_ours_open():
    from backend.airwallex.types import TransferPage

    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    scheduled = _extra("awx-sched", "SCHEDULED")
    client = _scan_client({"start": TransferPage(items=(scheduled,), page_after=None)}, "start")
    ref = _grant(conn, incident_id)

    _refused_untouched(conn, client, ref, incident_id, expect_state="DUPLICATE")


def test_a_repeated_continuation_fails_closed():
    from backend.airwallex.types import TransferPage

    conn = connect()
    _obligation_id, incident_id = _ready(conn)
    pages = {
        "start": TransferPage(items=(), page_after="1"),
        "1": TransferPage(items=(), page_after="1"),
    }
    client = _scan_client(pages, "start")
    ref = _grant(conn, incident_id)

    with pytest.raises(ReplaceError):
        execute(conn, client, ref.id, as_of=SEEN)
    assert client.created == []

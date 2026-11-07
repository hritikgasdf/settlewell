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
    def __init__(self, *, reason: str = "90701: Account closed", amount: str = "4200.00") -> None:
        self.reason = reason
        self.amount = amount
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
                    amount=Decimal("4200.00"),
                    currency="EUR",
                    balance=None,
                    description=None,
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
            return Balance(currency, value, Decimal(0), Decimal(0), value, Decimal(0))

        return [row("EUR", "0.00"), row("USD", "100000.00")]


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
    client = Pay()
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


def _intent_id(conn) -> int:
    return int(conn.execute("SELECT id FROM execution_intent ORDER BY id DESC LIMIT 1").fetchone()["id"])


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
    first = _grant(conn, incident_id, quote_id="quo_same")
    second = _grant(conn, incident_id, quote_id="quo_same")

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
            quote_id="quo_same",
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
    paid = replace_payment(
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
    assert paid == "awx-new"
    assert len(client.created) == 1


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

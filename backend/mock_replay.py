"""Offline replay of the hero. Statuses, the letter, the reversal, and the quote are canned.

Policy, approvals, the proof gate, the duplicate cancel, the bank-change gate, and
quote single-use are the real functions. Nothing here opens a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from html import escape

from backend.airwallex.types import (
    Balance,
    BalanceHistoryItem,
    BalanceHistoryPage,
    Beneficiary,
    BeneficiaryValidation,
    CancelResult,
    ConfirmationLetter,
    Conversion,
    FxQuote,
    Transfer,
    TransferPage,
)
from backend.approvals import (
    AP_CLERK,
    FINANCE_APPROVER,
    ApprovalError,
    approve,
)
from backend.bank_change import verify_bank_change
from backend.execute import CallbackConfirmation, execute, intent_terms, open_intent
from backend.fx_funding import fund_replacement
from backend.fx_quote import FxQuoteError, execute_fx_quote
from backend.ledger import append_bank_detail_version, connect, migrate
from backend.paid_once import scan_paid_once
from backend.policy import code_from_reason, decide
from backend.proof import issue_proof
from backend.reconcile import poll_incident
from backend.replace_payment import ReplaceError
from backend.replacement_tool import propose_replacement, refund_card
from backend.stop_duplicate import stop_duplicate

SEEN = "2026-11-03T12:00:00+00:00"
USED = "2026-11-03T12:10:00+00:00"
EXPIRES = "2026-11-04T12:00:00+00:00"
LETTER = b"NO_FEE_DISPLAY synthetic confirmation INV-2291"
HERO_ID = "awx-hero"
DUP_ID = "awx-dup"
REPLACE_ID = "awx-replace"
QUOTE_ID = "quo_hero"

_NEW_BANK = {
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


@dataclass(frozen=True)
class Beat:
    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class HeroReplay:
    beats: tuple[Beat, ...]
    duplicate_fee: str
    failure_rule: str
    proof_while_sent: bool
    proof_after_cancel: bool
    proof_refusal: str
    bank_card: str
    refund_text: str
    quote_id: str
    quote_state: str
    reuse_code: str
    proposer: str
    approver: str
    replacement_id: str
    created_count: int
    second_replacement: str
    paid_once: bool
    incident_cost: str
    eur_before: str
    eur_after: str
    usd_before: str
    usd_after: str


class HeroBook:
    """Canned Airwallex reads. The same inputs always return the same records."""

    def __init__(self) -> None:
        self.hero_status = "SENT"
        self.hero_reason: str | None = None
        self.dup_status = "SCHEDULED"
        self.reversal = False
        self.eur = Decimal("800.00")
        self.usd = Decimal("100000.00")
        self.sell = Decimal(0)
        self.created: list[Transfer] = []
        self.updated: list[dict] = []

    def get_transfer(self, transfer_id: str) -> Transfer:
        if transfer_id == HERO_ID:
            return self._hero()
        if transfer_id == DUP_ID:
            return self._dup()
        for item in self.created:
            if item.id == transfer_id:
                return item
        raise KeyError(transfer_id)

    def list_transfers(
        self,
        *,
        request_id: str | None = None,
        short_reference_id: str | None = None,
        page_size: int | None = None,
        page: str | None = None,
    ) -> TransferPage:
        del short_reference_id, page_size, page
        items = [self._hero(), self._dup(), *self.created]
        if request_id is not None:
            items = [item for item in items if item.request_id == request_id]
        return TransferPage(items=tuple(items), page_after=None)

    def cancel_transfer(self, transfer_id: str) -> CancelResult:
        if transfer_id == DUP_ID:
            self.dup_status = "CANCELLED"
        return CancelResult(ok=True, transfer=self.get_transfer(transfer_id))

    def create_confirmation_letter(
        self,
        *,
        transaction_id: str,
        letter_format: str,
    ) -> ConfirmationLetter:
        del transaction_id, letter_format
        return ConfirmationLetter(content=LETTER, media_type="application/pdf")

    def balance_history(
        self,
        *,
        currency: str,
        page_size: int = 10,
        page: str | None = None,
    ) -> BalanceHistoryPage:
        del page_size, page
        if not self.reversal or currency != "EUR":
            return BalanceHistoryPage(items=(), has_more=False, page_after=None)
        item = BalanceHistoryItem(
            id="rev-hero",
            source=HERO_ID,
            amount=Decimal("4200.00"),
            currency="EUR",
            balance=None,
            description="Refund",
            fee=Decimal(0),
            posted_at=SEEN,
            source_type="PAYOUT",
            transaction_type="PAYOUT_REVERSAL",
            account_type=None,
            reference="INV-2291",
        )
        return BalanceHistoryPage(items=(item,), has_more=False, page_after=None)

    def current_balances(self) -> list[Balance]:
        return [_balance("EUR", self.eur), _balance("USD", self.usd)]

    def create_quote(
        self,
        *,
        buy_currency: str,
        sell_currency: str,
        validity: str,
        buy_amount: Decimal | None = None,
        sell_amount: Decimal | None = None,
    ) -> FxQuote:
        del sell_currency, validity, sell_amount
        buy = buy_amount if buy_amount is not None else Decimal(0)
        self.sell = (buy * Decimal("1.10")).quantize(Decimal("0.01"))
        return FxQuote(
            quote_id=QUOTE_ID,
            client_rate=Decimal("1.10"),
            buy_currency=buy_currency,
            sell_currency="USD",
            buy_amount=buy,
            sell_amount=self.sell,
            valid_to_at="2026-11-03T12:15:00+00:00",
            usage="MULTI_USE",
        )

    def create_conversion(
        self,
        *,
        quote_id: str,
        buy_currency: str,
        sell_currency: str,
        buy_amount: Decimal | None = None,
        sell_amount: Decimal | None = None,
        request_id: str | None = None,
    ) -> Conversion:
        del buy_currency, sell_currency, sell_amount, request_id
        self.eur += buy_amount if buy_amount is not None else Decimal(0)
        self.usd -= self.sell
        return Conversion(conversion_id="con_hero", status="SETTLED", quote_id=quote_id)

    def validate_beneficiary(self, body: dict) -> BeneficiaryValidation:
        del body
        return BeneficiaryValidation(ok=True)

    def create_beneficiary(self, body: dict) -> Beneficiary:
        del body
        return Beneficiary(id="ben_dkb", nickname="Lumen DKB")

    def update_beneficiary(self, body: dict) -> Beneficiary:
        self.updated.append(dict(body))
        raise AssertionError("a bank change creates a new beneficiary")

    def create_transfer(
        self,
        *,
        transfer_amount: Decimal,
        transfer_currency: str,
        source_currency: str,
        beneficiary_id: str,
        transfer_method: str,
        reference: str,
        reason: str,
        request_id: str,
        transfer_date: str | None = None,
        metadata: dict | None = None,
    ) -> Transfer:
        del source_currency, transfer_method, reason, transfer_date, metadata
        transfer = Transfer(
            id=REPLACE_ID,
            status="PAID",
            request_id=request_id,
            transfer_amount=transfer_amount,
            transfer_currency=transfer_currency,
            fee_amount=Decimal("0.00"),
            reference=reference,
            beneficiary_id=beneficiary_id,
            transfer_method="SWIFT",
        )
        self.created.append(transfer)
        return transfer

    def _hero(self) -> Transfer:
        return Transfer(
            id=HERO_ID,
            status=self.hero_status,
            request_id="original-payment",
            transfer_amount=Decimal("4200.00"),
            transfer_currency="EUR",
            fee_amount=Decimal("13.47"),
            fee_currency="EUR",
            short_reference_id="P2291",
            reference="INV-2291",
            beneficiary_id="ben_commerzbank",
            transfer_date="2026-10-28",
            dispatch_date="2026-10-30",
            transfer_method="SWIFT",
            failure_reason=self.hero_reason,
        )

    def _dup(self) -> Transfer:
        return Transfer(
            id=DUP_ID,
            status=self.dup_status,
            request_id="colleague-friday",
            transfer_amount=Decimal("4200.00"),
            transfer_currency="EUR",
            fee_amount=Decimal("0.00"),
            reference="INV-2291",
            beneficiary_id="ben_commerzbank",
            transfer_method="SWIFT",
        )


def play_hero(conn=None) -> HeroReplay:
    """Step through the hero on an in-memory ledger. Two calls return the same replay."""
    own = conn is None
    if conn is None:
        conn = connect()
    try:
        return _play(conn, HeroBook())
    finally:
        if own:
            conn.close()


def render_replay(conn=None) -> str:
    """HTML for /replay. Output is escaped. The banner says MOCK."""
    return _page(play_hero(conn))


def _play(conn, book: HeroBook) -> HeroReplay:
    obligation_id, incident_id, _transfer_id = _seed(conn)
    beats: list[Beat] = [
        Beat(
            "Cold open",
            (
                (
                    "Lumen Textiles GmbH: EUR 4,200 for INV-2291 never arrived. "
                    "Pay to our NEW account today."
                ),
            ),
        )
    ]

    opened = scan_paid_once(conn, book, obligation_id)
    dup_incident = opened[0]
    dup_row = conn.execute(
        "SELECT id, amount, currency, details_hash FROM transfer_record WHERE airwallex_id = ?",
        (DUP_ID,),
    ).fetchone()
    beats.append(
        Beat(
            "Investigate",
            (
                "Original awx-hero is SENT, dispatched late.",
                "A second EUR 4,200 for INV-2291 is SCHEDULED by a colleague, outside Settlewell.",
            ),
        )
    )

    duplicate = decide("SENT", claim="duplicate", duplicate_status="SCHEDULED")
    remote_dup = book.get_transfer(DUP_ID)
    cancel_intent = open_intent(
        conn,
        kind="cancel",
        incident_id=dup_incident,
        obligation_id=obligation_id,
        action="STOP_DUPLICATE",
        amount=str(dup_row["amount"]),
        currency=str(dup_row["currency"]),
        beneficiary_id=remote_dup.beneficiary_id or "",
        details_hash=dup_row["details_hash"] or "",
        proposer=AP_CLERK,
        target_id=str(dup_row["id"]),
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(
        conn,
        cancel_intent.approval_id,
        intent_terms(conn, cancel_intent.id),
        approver=FINANCE_APPROVER,
        used_at=USED,
    )
    stop_approval = cancel_intent.approval_id
    stopped = stop_duplicate(
        conn,
        book,
        approval_id=stop_approval,
        transfer_record_id=dup_row["id"],
        as_of=USED,
    )
    conn.execute("UPDATE transfer_record SET fee = '0.00' WHERE airwallex_id = ?", (DUP_ID,))
    conn.commit()
    beats.append(
        Beat(
            "Stop the duplicate",
            (
                f"{duplicate.action.value} {duplicate.rule_id}.",
                f"Finance approver approved the cancel. Status {stopped.status}. Cancelled at €0.",
            ),
        )
    )

    sent_proof = issue_proof(conn, book, _transfer_id, incident_id=incident_id, seen_at=SEEN)
    beats.append(
        Beat(
            "Proof while SENT",
            (
                f"Proof attached: {sent_proof.attached}. Rule {sent_proof.rule_id}.",
                sent_proof.eta,
                sent_proof.cutoff_label or "",
            ),
        )
    )

    book.hero_status = "CANCELLED"
    book.hero_reason = "90701: Account closed"
    poll_incident(conn, book, incident_id, seen_at=SEEN)
    failed = book.get_transfer(HERO_ID)
    failure = decide(
        failed.status,
        failure_code=code_from_reason(failed.failure_reason),
        claim="new_details",
    )
    beats.append(
        Beat(
            "Failure code",
            (f"{failed.failure_reason} → {failure.action.value} {failure.rule_id}.",),
        )
    )

    refused = issue_proof(conn, book, _transfer_id, incident_id=incident_id, seen_at=USED)
    beats.append(
        Beat(
            "Proof refused",
            (refused.reply_draft,),
        )
    )

    card = verify_bank_change(
        conn,
        book,
        obligation_id=obligation_id,
        incident_ref=str(incident_id),
        bank_name="Deutsche Kreditbank",
        account_last4="7781",
        beneficiary=_NEW_BANK,
        confirmation=CallbackConfirmation(
            phone="+49-30-000000",
            confirmed=True,
            attested_by=FINANCE_APPROVER,
            attested_at=SEEN,
        ),
    )
    bank_intent = conn.execute(
        "SELECT id FROM execution_intent WHERE kind = 'beneficiary' ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    approve(
        conn,
        card.approval_id,
        intent_terms(conn, bank_intent),
        approver=FINANCE_APPROVER,
        used_at=USED,
    )
    created_bank = execute(conn, book, bank_intent, as_of=SEEN)
    beats.append(Beat("Bank change", (card.text,)))

    locked = refund_card(conn, book, obligation_id=obligation_id)
    book.reversal = True
    refund = refund_card(conn, book, obligation_id=obligation_id)
    beats.append(
        Beat(
            "Money back first",
            (
                f"Replacement locked until the reversal: {not locked.unlocked}.",
                refund.text or "",
            ),
        )
    )

    funding = fund_replacement(conn, book, obligation_id=obligation_id, as_of=SEEN)
    quote = conn.execute(
        "SELECT quote_id, pair, rate, amount, state FROM fx_quote WHERE quote_id = ?",
        (funding.quote_id,),
    ).fetchone()
    fx_intent = open_intent(
        conn,
        kind="fx_conversion",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="FX_FUND",
        amount=quote["amount"],
        currency="EUR",
        beneficiary_id=created_bank.remote_id,
        details_hash=card.details_hash,
        proposer=AP_CLERK,
        quote_id=quote["quote_id"],
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(
        conn,
        fx_intent.approval_id,
        intent_terms(conn, fx_intent.id),
        approver=FINANCE_APPROVER,
        used_at=USED,
    )
    execute(conn, book, fx_intent.id, as_of=SEEN)
    quote = conn.execute(
        "SELECT quote_id, pair, rate, state FROM fx_quote WHERE quote_id = ?",
        (funding.quote_id,),
    ).fetchone()
    beats.append(
        Beat(
            "USD to EUR",
            (
                f"{quote['pair']} locked quote {quote['quote_id']} rate {quote['rate']}.",
                f"Funding converted. Quote {quote['state']}.",
            ),
        )
    )

    recorded = book.get_transfer(HERO_ID)
    replace_intent = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action=failure.action.value,
        amount=format(recorded.transfer_amount, "f"),
        currency=recorded.transfer_currency or "EUR",
        beneficiary_id=created_bank.remote_id,
        details_hash=card.details_hash,
        proposer=AP_CLERK,
        failure_code="90701",
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    replace_terms = intent_terms(conn, replace_intent.id)
    approval_id = replace_intent.approval_id
    try:
        approve(conn, approval_id, replace_terms, approver=AP_CLERK, used_at=USED)
    except ApprovalError as error:
        self_approval = error.code
    else:
        self_approval = "approved"
    approve(conn, approval_id, replace_terms, approver=FINANCE_APPROVER, used_at=USED)
    reuse = open_intent(
        conn,
        kind="fx_conversion",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action="FX_FUND",
        amount=format(funding.buy_amount or Decimal(0), "f"),
        currency="EUR",
        beneficiary_id=created_bank.remote_id,
        details_hash=card.details_hash,
        proposer=AP_CLERK,
        quote_id=quote["quote_id"],
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(
        conn,
        reuse.approval_id,
        intent_terms(conn, reuse.id),
        approver=FINANCE_APPROVER,
        used_at=USED,
    )
    try:
        execute_fx_quote(
            conn,
            book,
            reuse.approval_id,
            intent_terms(conn, reuse.id),
            as_of=SEEN,
            intent_id=reuse.id,
        )
    except FxQuoteError as error:
        reuse_code = error.code
    else:
        reuse_code = "converted"
    replacement_id = propose_replacement(
        conn,
        book,
        obligation_id=obligation_id,
        incident_ref=str(incident_id),
        intent_id=replace_intent.id,
        as_of=SEEN,
    )
    second_intent = open_intent(
        conn,
        kind="replacement",
        incident_id=incident_id,
        obligation_id=obligation_id,
        action=failure.action.value,
        amount=format(recorded.transfer_amount, "f"),
        currency=recorded.transfer_currency or "EUR",
        beneficiary_id=created_bank.remote_id,
        details_hash=card.details_hash,
        proposer=AP_CLERK,
        failure_code="90701",
        expires_at=EXPIRES,
        created_at=SEEN,
    )
    approve(
        conn,
        second_intent.approval_id,
        intent_terms(conn, second_intent.id),
        approver=FINANCE_APPROVER,
        used_at=USED,
    )
    try:
        propose_replacement(
            conn,
            book,
            obligation_id=obligation_id,
            incident_ref=str(incident_id),
            intent_id=second_intent.id,
            as_of=SEEN,
        )
    except ReplaceError as error:
        second = error.code
    else:
        second = "same"
    stored = conn.execute(
        "SELECT proposer, approver FROM approval WHERE id = ?",
        (approval_id,),
    ).fetchone()
    beats.append(
        Beat(
            "One approval",
            (
                f"Self-approval {self_approval}.",
                f"Proposer {stored['proposer']}. Approver {stored['approver']}.",
                f"Quote reuse rejected: {reuse_code}.",
                f"Replacement {replacement_id}. Second request {second}.",
            ),
        )
    )

    poll_incident(conn, book, incident_id, seen_at=USED)
    obligation = conn.execute(
        "SELECT invoice_ref, status FROM obligation WHERE id = ?",
        (obligation_id,),
    ).fetchone()
    fee = conn.execute(
        "SELECT fee FROM transfer_record WHERE airwallex_id = ?",
        (HERO_ID,),
    ).fetchone()["fee"]
    dup_fee = conn.execute(
        "SELECT fee, status FROM transfer_record WHERE airwallex_id = ?",
        (DUP_ID,),
    ).fetchone()
    balances = {
        (row["currency"], row["label"]): row["available"]
        for row in conn.execute("SELECT currency, label, available FROM balance_snapshot")
    }
    beats.append(
        Beat(
            "Ledger",
            (
                f"{obligation['invoice_ref']} {obligation['status'].replace('_', ' ').lower()}.",
                f"Incident cost €{fee}. Duplicate fee €{dup_fee['fee']}.",
                f"EUR before {balances[('EUR', 'before')]} after {balances[('EUR', 'after')]}.",
                f"USD before {balances[('USD', 'before')]} after {balances[('USD', 'after')]}.",
            ),
        )
    )
    return HeroReplay(
        beats=tuple(beats),
        duplicate_fee=dup_fee["fee"],
        failure_rule=failure.rule_id,
        proof_while_sent=sent_proof.attached,
        proof_after_cancel=refused.attached,
        proof_refusal=refused.reply_draft,
        bank_card=card.text,
        refund_text=refund.text or "",
        quote_id=quote["quote_id"],
        quote_state=quote["state"],
        reuse_code=reuse_code,
        proposer=stored["proposer"],
        approver=stored["approver"],
        replacement_id=replacement_id,
        created_count=len(book.created),
        second_replacement=second,
        paid_once=obligation["status"] == "PAID_ONCE",
        incident_cost=fee,
        eur_before=balances[("EUR", "before")],
        eur_after=balances[("EUR", "after")],
        usd_before=balances[("USD", "before")],
        usd_after=balances[("USD", "after")],
    )


def _seed(conn) -> tuple[int, int, int]:
    migrate(conn)
    supplier = conn.execute(
        "INSERT INTO supplier (name, contact, phone_on_file) VALUES (?, ?, ?)",
        ("Lumen Textiles GmbH", "ap@lumen.example", "+49-30-000000"),
    )
    append_bank_detail_version(
        conn,
        supplier_id=int(supplier.lastrowid),
        beneficiary_id="ben_commerzbank",
        details_hash="onboarding-v1",
        bank_name="Commerzbank",
        account_last4="4400",
        source="onboarding",
    )
    obligation = conn.execute(
        """
        INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status)
        VALUES ('INV-2291', '4200.00', 'EUR', ?, 'OPEN')
        """,
        (supplier.lastrowid,),
    )
    transfer = conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, obligation_id, origin, details_hash,
            amount, currency, fee, status, reference
        ) VALUES (?, 'original-payment', ?, 'settlewell', 'onboarding-v1',
                  '4200.00', 'EUR', '13.47', 'SENT', 'INV-2291')
        """,
        (HERO_ID, obligation.lastrowid),
    )
    conn.execute(
        "UPDATE obligation SET live_transfer_id = ? WHERE id = ?",
        (transfer.lastrowid, obligation.lastrowid),
    )
    incident = conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, ?, 'OPEN')",
        (
            obligation.lastrowid,
            "EUR 4,200 for INV-2291 never arrived. Pay to our NEW account today.",
        ),
    )
    conn.commit()
    return int(obligation.lastrowid), int(incident.lastrowid), int(transfer.lastrowid)


def _balance(currency: str, available: Decimal) -> Balance:
    return Balance(
        currency=currency,
        available_amount=available,
        pending_amount=Decimal(0),
        reserved_amount=Decimal(0),
        total_amount=available,
        prepayment_amount=Decimal(0),
    )


def _page(replay: HeroReplay) -> str:
    blocks = []
    for beat in replay.beats:
        lines = "".join(f"<li>{escape(line)}</li>" for line in beat.lines if line)
        blocks.append(f"<section><h2>{escape(beat.title)}</h2><ul>{lines}</ul></section>")
    fidelity = """
    <table id="fidelity">
      <thead><tr><th>Replayed</th><th>Real code</th></tr></thead>
      <tbody>
        <tr>
          <td>Canned transfer status, fee, confirmation letter, PAYOUT_REVERSAL, and quote rate.</td>
          <td>Policy, second-person approvals, the proof gate, duplicate cancel,
              bank-change validate and callback, and FX single-use.</td>
        </tr>
        <tr>
          <td>ETA and cut-off are labels, not Airwallex API fields.</td>
          <td>The replay does not call Airwallex.</td>
        </tr>
      </tbody>
    </table>
    """
    body = (
        '<p class="banner">MOCK</p>'
        '<p><a href="/">Inbox</a></p>'
        "<h1>Hero replay</h1>"
        f"{''.join(blocks)}"
        "<section><h2>Mock fidelity</h2>"
        f"{fidelity}</section>"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Hero replay · Settlewell</title>
</head>
<body>
{body}
</body>
</html>
"""

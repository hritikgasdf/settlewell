"""Bounded, deterministic checks that the money invariants hold. No network."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.airwallex.types import BalanceHistoryItem, BalanceHistoryPage, Transfer, TransferPage
from backend.approvals import AP_CLERK, FINANCE_APPROVER, ApprovalError, approve
from backend.execute import ExecuteError, execute, intent_terms, open_intent
from backend.ledger import append_bank_detail_version, connect, migrate
from backend.proof import issue_proof
from backend.replacement_tool import ReplaceError

_STATUSES = ("SCHEDULED", "PROCESSING", "SENT", "PAID", "FAILED", "CANCELLED")
_LIVE = frozenset({"SCHEDULED", "PROCESSING", "SENT", "PAID", "OVERDUE"})
_SECRET = "property-secret"
_NOW = 1_700_000_000_000


class _World:
    def __init__(self) -> None:
        self.conn = connect()
        migrate(self.conn)
        supplier = self.conn.execute(
            "INSERT INTO supplier (name, contact, phone_on_file) VALUES (?, ?, ?)",
            ("Example Mills", "ap@example.test", "+00-00-000000"),
        )
        append_bank_detail_version(
            self.conn,
            supplier_id=int(supplier.lastrowid),
            beneficiary_id="ben_example",
            details_hash="details-v1",
            bank_name="Example Bank",
            account_last4="0000",
            source="verified_callback",
            verified_by="finance-approver",
            verified_at="2026-11-03T12:00:00+00:00",
        )
        obligation = self.conn.execute(
            """
            INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status)
            VALUES ('INV-3300', '100.00', 'EUR', ?, 'OPEN')
            """,
            (supplier.lastrowid,),
        )
        self.obligation_id = int(obligation.lastrowid)
        transfer = self.conn.execute(
            """
            INSERT INTO transfer_record (
                airwallex_id, request_id, obligation_id, origin, amount, currency,
                status, reference, details_hash
            ) VALUES ('awx-prop', 'original-payment', ?, 'settlewell', '100.00', 'EUR',
                      'CANCELLED', 'INV-3300', 'details-v1')
            """,
            (self.obligation_id,),
        )
        self.transfer_id = int(transfer.lastrowid)
        self.conn.execute(
            "UPDATE obligation SET live_transfer_id = ? WHERE id = ?",
            (self.transfer_id, self.obligation_id),
        )
        incident = self.conn.execute(
            "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, NULL, 'OPEN')",
            (self.obligation_id,),
        )
        self.incident_id = int(incident.lastrowid)
        opened = open_intent(
            self.conn,
            kind="replacement",
            incident_id=self.incident_id,
            obligation_id=self.obligation_id,
            action="REPLACE_SAME",
            amount="100.00",
            currency="EUR",
            beneficiary_id="ben_example",
            details_hash="details-v1",
            proposer=AP_CLERK,
            failure_code="90802",
            expires_at="2026-11-04T12:00:00+00:00",
            created_at="2026-11-03T12:00:00+00:00",
        )
        self.intent_id = opened.id
        self.approval_id = opened.approval_id
        self.client = _Client()
        self.events = 0
        self.last: tuple[bytes, str, str] | None = None
        self.conn.commit()

    def apply(self, kind: str, status: str | None) -> None:
        if kind == "webhook":
            self._event(status or "SENT", replay=False)
        elif kind == "replay" and self.last is not None:
            raw, stamp, signature = self.last
            receive(self.conn, raw, stamp, signature)
        elif kind == "self":
            self._approve(AP_CLERK)
        elif kind == "other" or kind == "again":
            self._approve(FINANCE_APPROVER)
        elif kind == "pay":
            self._pay()
        elif kind == "proof":
            self.client.remote_status = status or "SENT"
            issue_proof(
                self.conn,
                self.client,
                self.transfer_id,
                incident_id=self.incident_id,
                seen_at="2026-11-03T12:00:00+00:00",
            )
        elif kind == "colleague":
            self.client.colleague = True
            self._pay()

    def _approve(self, approver: str) -> None:
        try:
            approve(
                self.conn,
                self.approval_id,
                intent_terms(self.conn, self.intent_id),
                approver=approver,
                used_at="2026-11-03T12:05:00+00:00",
            )
        except ApprovalError:
            return

    def _pay(self) -> None:
        approval = self.conn.execute(
            "SELECT approver, proposer, used_at FROM approval WHERE id = ?",
            (self.approval_id,),
        ).fetchone()
        if approval["used_at"] is None or approval["approver"] == approval["proposer"]:
            return
        try:
            execute(self.conn, self.client, self.intent_id, as_of="2026-11-03T12:10:00+00:00")
        except (ReplaceError, ExecuteError):
            return

    def _event(self, status: str, *, replay: bool) -> None:
        if replay and self.last is not None:
            raw, stamp, signature = self.last
        else:
            self.events += 1
            body = {
                "id": f"evt-{self.events}",
                "name": f"payout.transfer.{status.lower()}",
                "created_at": datetime.fromtimestamp(_NOW / 1000 + self.events, UTC).isoformat(),
                "data": {"id": "awx-prop", "status": status},
            }
            raw = json.dumps(body, separators=(",", ":")).encode()
            stamp = str(_NOW)
            signature = hmac.new(_SECRET.encode(), stamp.encode() + raw, hashlib.sha256).hexdigest()
            self.last = (raw, stamp, signature)
        receive(self.conn, raw, stamp, signature)

    def check(self) -> None:
        ours = self.conn.execute(
            """
            SELECT status FROM transfer_record
            WHERE obligation_id = ? AND origin = 'settlewell' AND status IN
            ('SCHEDULED', 'PROCESSING', 'SENT', 'PAID', 'OVERDUE')
            """,
            (self.obligation_id,),
        ).fetchall()
        assert len(ours) <= 1
        created = self.conn.execute(
            "SELECT id FROM transfer_record WHERE request_id LIKE 'stl-%'"
        ).fetchall()
        if created:
            approval = self.conn.execute(
                "SELECT proposer, approver, used_at FROM approval WHERE id = ?",
                (self.approval_id,),
            ).fetchone()
            assert approval["used_at"] is not None
            assert approval["approver"] != approval["proposer"]
            assert len(self.client.created) <= 1
        for proof in self.conn.execute("SELECT status_at_issue FROM proof_document"):
            assert proof["status_at_issue"] in {"SENT", "PAID"}


class _Client:
    def __init__(self) -> None:
        self.remote_status = "CANCELLED"
        self.created: list[str] = []
        self.colleague = False

    def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer(
            id=transfer_id,
            status=self.remote_status,
            transfer_amount=Decimal("100.00"),
            transfer_currency="EUR",
            transfer_method="SWIFT",
            fee_amount=Decimal(0),
            failure_reason="90802: Beneficiary bank returned",
            reference="INV-3300",
        )

    def balance_history(self, **_kwargs) -> BalanceHistoryPage:
        return BalanceHistoryPage(
            items=(
                BalanceHistoryItem(
                    id="rev-1",
                    source="awx-prop",
                    amount=Decimal("100.00"),
                    currency="EUR",
                    balance=None,
                    description=None,
                    fee=Decimal(0),
                    posted_at=None,
                    source_type="PAYOUT",
                    transaction_type="PAYOUT_REVERSAL",
                    account_type="CASH",
                    reference=None,
                ),
            ),
            has_more=False,
            page_after=None,
        )

    def list_transfers(self, **_kwargs) -> TransferPage:
        if not self.colleague:
            return TransferPage(items=(), page_after=None)
        return TransferPage(
            items=(
                Transfer(
                    id="awx-colleague",
                    status="PAID",
                    request_id="colleague-hand",
                    transfer_amount=Decimal("100.00"),
                    transfer_currency="EUR",
                    reference="INV-3300",
                    beneficiary_id="ben_other",
                ),
            ),
            page_after=None,
        )

    def create_transfer(self, **kwargs):
        self.created.append(kwargs["request_id"])
        return Transfer(
            id="awx-one",
            status="SCHEDULED",
            request_id=kwargs["request_id"],
            transfer_amount=Decimal(kwargs["transfer_amount"]),
            transfer_currency=kwargs["transfer_currency"],
            reference="INV-3300",
        )

    def create_confirmation_letter(self, **_kwargs):
        from backend.airwallex.types import ConfirmationLetter

        return ConfirmationLetter(content=b"%PDF", media_type="application/pdf")


def receive(conn, raw: bytes, stamp: str, signature: str) -> None:
    from backend.webhooks import receive_webhook

    receive_webhook(
        conn,
        secret=_SECRET,
        raw_body=raw,
        x_timestamp=stamp,
        x_signature=signature,
        now_ms=_NOW,
    )


@settings(max_examples=20, derandomize=True, deadline=None)
@given(
    steps=st.lists(
        st.tuples(
            st.sampled_from(("webhook", "replay", "self", "other", "again", "pay", "proof", "colleague")),
            st.sampled_from(_STATUSES),
        ),
        min_size=1,
        max_size=6,
    )
)
def test_money_invariants_hold_for_mixed_sequences(steps):
    world = _World()
    for kind, status in steps:
        world.apply(kind, status)
        world.check()

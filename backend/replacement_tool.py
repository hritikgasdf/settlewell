"""The only replacement tool. Callers pass an obligation and an incident, never an amount."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.airwallex.types import BalanceHistoryPage
from backend.policy import Action, code_from_reason, decide
from backend.repaid import colleague_repaid
from backend.replace_payment import ReplaceError, replace_payment, replacement_request_id


@dataclass(frozen=True)
class RefundCard:
    unlocked: bool
    text: str | None


def refund_card(conn, client, *, obligation_id: int) -> RefundCard:
    """Show the returned principal and the lost fee. Replace stays locked until then."""
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if obligation is None or obligation["live_transfer_id"] is None:
        return RefundCard(False, None)
    original = conn.execute(
        "SELECT * FROM transfer_record WHERE id = ?",
        (obligation["live_transfer_id"],),
    ).fetchone()
    if original is None or not original["airwallex_id"]:
        return RefundCard(False, None)
    remote = client.get_transfer(original["airwallex_id"])
    reversal = _reversal_for(client, original["airwallex_id"], remote.transfer_currency)
    if reversal is None or reversal.amount is None:
        return RefundCard(False, None)
    fee = remote.fee_amount if remote.fee_amount is not None else Decimal(0)
    principal = _money(reversal.currency or remote.transfer_currency, abs(Decimal(reversal.amount)))
    lost = _money(remote.fee_currency or remote.transfer_currency, Decimal(fee))
    text = f"principal back: {principal} · fee lost {lost}"
    return RefundCard(True, text)


def propose_replacement(
    conn,
    client,
    *,
    obligation_id: int,
    incident_ref: str,
    intent_id: int | None = None,
    as_of: str | None = None,
) -> str:
    incident = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_ref,)).fetchone()
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if incident is None or obligation is None or incident["obligation_id"] != obligation_id:
        raise ReplaceError("incident")
    if incident["state"] == "CLOSED":
        raise ReplaceError("repaid")
    card = refund_card(conn, client, obligation_id=obligation_id)
    if not card.unlocked:
        raise ReplaceError("refund")
    decision = colleague_repaid(conn, client, obligation_id=obligation_id, incident_ref=incident_ref)
    if decision.action != "replace":
        raise ReplaceError("repaid")
    original = conn.execute(
        "SELECT * FROM transfer_record WHERE id = ?",
        (obligation["live_transfer_id"],),
    ).fetchone()
    if original is None or not original["airwallex_id"]:
        raise ReplaceError("original")
    remote = client.get_transfer(original["airwallex_id"])
    if remote.transfer_amount is None or not remote.transfer_currency:
        raise ReplaceError("record")
    version = conn.execute(
        """
        SELECT beneficiary_id, details_hash FROM bank_detail_version
        WHERE supplier_id = ? AND source = 'verified_callback' AND beneficiary_id IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (obligation["supplier_id"],),
    ).fetchone()
    if version is None:
        raise ReplaceError("unverified")
    decision = decide(
        remote.status,
        failure_code=code_from_reason(remote.failure_reason),
        claim="where",
    )
    if decision.action not in {Action.REPLACE_SAME, Action.REPLACE_NEW_DETAILS}:
        raise ReplaceError("policy")
    if intent_id is None or as_of is None:
        raise ReplaceError("approval")
    intent = conn.execute("SELECT * FROM execution_intent WHERE id = ?", (intent_id,)).fetchone()
    if intent is None or intent["approval_id"] is None:
        raise ReplaceError("approval")
    if int(intent["obligation_id"] or 0) != obligation_id:
        raise ReplaceError("obligation")
    return replace_payment(
        conn,
        client,
        obligation_id=obligation_id,
        attempt=_next_attempt(conn, obligation["invoice_ref"]),
        beneficiary_id=version["beneficiary_id"],
        details_hash=version["details_hash"],
        transfer_method=remote.transfer_method or "SWIFT",
        reason="other_services",
        transfer_amount=Decimal(remote.transfer_amount),
        transfer_currency=remote.transfer_currency,
        as_of=as_of,
        intent_id=int(intent["id"]),
        approval_id=int(intent["approval_id"]),
        quote_id=intent["quote_id"],
    )


def _next_attempt(conn, invoice_ref: str) -> int:
    prefix = replacement_request_id(invoice_ref, 1).rsplit("-", 1)[0] + "-"
    rows = conn.execute(
        "SELECT request_id FROM transfer_record WHERE request_id LIKE ?",
        (prefix + "%",),
    ).fetchall()
    numbers = []
    for row in rows:
        suffix = row["request_id"][len(prefix) :]
        if suffix.isdigit():
            numbers.append(int(suffix))
    return max(numbers, default=0) + 1


def _reversal_for(client, transfer_id: str, currency: str | None):
    if not currency:
        return None
    page_token = None
    for _ in range(5):
        history: BalanceHistoryPage = client.balance_history(
            currency=currency,
            page_size=10,
            page=page_token,
        )
        for item in history.items:
            if item.transaction_type == "PAYOUT_REVERSAL" and item.source == transfer_id:
                return item
        if not history.has_more or not history.page_after:
            return None
        page_token = history.page_after
    return None


_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def _money(currency: str | None, amount: Decimal) -> str:
    """Format an amount in the transfer currency. EUR stays €4,200 / €13.47."""
    code = (currency or "").upper()
    if amount == amount.to_integral_value():
        shown = f"{int(amount):,}"
    else:
        shown = f"{amount:,.2f}"
    symbol = _SYMBOLS.get(code)
    if symbol:
        return f"{symbol}{shown}"
    prefix = f"{code} " if code else ""
    return f"{prefix}{shown}"

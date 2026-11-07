"""The only replacement tool. Callers pass an obligation and an incident, never an amount."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.outcomes import HistoryError, full_refund, refund_currency, reversals_for
from backend.paid_once import find_matches, scan_paid_once
from backend.policy import Action, code_from_reason, decide
from backend.repaid import _unresolved_tracked, colleague_repaid, open_extra, terminal_ids
from backend.replace_payment import (
    ReplaceError,
    replace_payment,
    replacement_request_id,
    same_details_target,
)


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
    try:
        reversals = reversals_for(client, original["airwallex_id"], refund_currency(remote))
    except HistoryError:
        return RefundCard(False, None)
    reversal = full_refund(remote, reversals)
    if reversal is None:
        return RefundCard(False, None)
    fee = remote.fee_amount
    principal = _money(reversal.currency or remote.transfer_currency, Decimal(reversal.amount))
    if fee is None or not Decimal(fee).is_finite() or Decimal(fee) < 0:
        lost = "unknown"  # the payout gate refuses an unknown fee; the card never shows it as zero
    else:
        lost = _money(remote.fee_currency or remote.transfer_currency, Decimal(fee))
    text = f"principal back: {principal} · fee lost {lost}"
    return RefundCard(True, text)


def _own_attempt_recorded(conn, intent_id: int | None) -> bool:
    """Our stored request id is recovered by replace_payment, which validates ownership read-only first.
    The colleague scan must not see our accepted payment as external."""
    if intent_id is None:
        return False
    row = conn.execute(
        """
        SELECT transfer_record.id FROM execution_intent
        JOIN transfer_record ON transfer_record.request_id = execution_intent.request_id
        WHERE execution_intent.id = ? AND execution_intent.state = 'executed'
        """,
        (intent_id,),
    ).fetchone()
    return row is not None


def _refuse_on_scan(conn, client, obligation_id: int, incident_ref: str) -> None:
    """One complete supplier-aware scan. Any match or ambiguity refuses before the payout."""
    dead = terminal_ids(conn, obligation_id)  # a stored FAILED/CANCELLED outranks a stale live listing
    matches = [(kind, remote) for kind, remote in find_matches(conn, client, obligation_id) if remote.id not in dead]
    if not matches and not _unresolved_tracked(conn, int(incident_ref)) and not open_extra(conn, client, obligation_id):
        return
    decision = colleague_repaid(
        conn, client, obligation_id=obligation_id, incident_ref=incident_ref, matches=matches
    )
    if decision.action == "replace":
        if not matches:
            return  # every tracked payment is now final and not paid
        # Original not yet cancelled or unlinked: still record the matches, never send.
        scan_paid_once(conn, client, obligation_id, matches)
    raise ReplaceError("repaid")


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
    if not _own_attempt_recorded(conn, intent_id):
        # A request we already recorded recovers first, by request id, without the refund or funding again.
        card = refund_card(conn, client, obligation_id=obligation_id)
        if not card.unlocked:
            raise ReplaceError("refund")
        _refuse_on_scan(conn, client, obligation_id, incident_ref)
    original = conn.execute(
        "SELECT * FROM transfer_record WHERE id = ?",
        (obligation["live_transfer_id"],),
    ).fetchone()
    if original is None or not original["airwallex_id"]:
        raise ReplaceError("original")
    remote = client.get_transfer(original["airwallex_id"])
    if remote.transfer_amount is None or not remote.transfer_currency:
        raise ReplaceError("record")
    decision = decide(
        remote.status,
        failure_code=code_from_reason(remote.failure_reason),
        claim="where",
    )
    if decision.action is Action.REPLACE_SAME:
        beneficiary_id, details_hash = same_details_target(conn, obligation, remote)
    elif decision.action is Action.REPLACE_NEW_DETAILS:
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
        beneficiary_id = version["beneficiary_id"]
        details_hash = version["details_hash"]
    else:
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
        attempt=_attempt_for(conn, obligation["invoice_ref"], int(intent["id"])),
        beneficiary_id=beneficiary_id,
        details_hash=details_hash,
        transfer_method=remote.transfer_method or "SWIFT",
        reason="other_services",
        transfer_amount=Decimal(remote.transfer_amount),
        transfer_currency=remote.transfer_currency,
        as_of=as_of,
        intent_id=int(intent["id"]),
        approval_id=int(intent["approval_id"]),
        quote_id=intent["quote_id"],
    )


def _attempt_for(conn, invoice_ref: str, intent_id: int) -> int:
    """Resume the request id stored before the call. Do not mint the next attempt.

    The stored id is read again under the write lock. A copy read earlier can be stale:
    a concurrent worker for this same intent may have recorded attempt 1 since."""
    prefix = replacement_request_id(invoice_ref, 1).rsplit("-", 1)[0] + "-"
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT request_id FROM execution_intent WHERE id = ?", (intent_id,)
        ).fetchone()
        recorded = row["request_id"] if row is not None else None
        if recorded and recorded.startswith(prefix):
            suffix = recorded[len(prefix) :]
            if suffix.isdigit():
                return int(suffix)
        return _next_attempt(conn, invoice_ref)
    finally:
        conn.rollback()


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

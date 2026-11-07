"""Payout funding gate and durable ledger. A new payout needs a full refund and live cash first.

The gate runs under the replacement's write lock, after the exact-intent checks and before the
claim is stored. A refusal leaves the intent proposed and the approval usable.

Fee budget: the adapter has no verified pre-send fee endpoint. Unless a client supplies an optional,
read-only `quote_payout_fee`, the budget is a coded conservative estimate,
`conservative_upper_bound_estimate` = max(2 x the live original fee, 25.00 payout-currency units) for
EUR/USD/GBP. It is not a bank-enforced cap and cannot guarantee future bank pricing. A quote that is
present but malformed, expired, errored or for other terms holds the payout; it never falls back.
A larger actual fee is audited and escalated, never resent.

Holds: every unresolved or accepted send keeps its amount plus fee budget out of the live balance
until a definitive first-send refusal releases it. A read never releases a hold, because no debit
attribution proves the payout is already inside the balance the next sender would read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from backend.airwallex.client import AirwallexError
from backend.fx_funding import FundingError, reserve_floor
from backend.ledger import append_audit, set_obligation_status
from backend.outcomes import HistoryError, full_refund, refund_currency, reversals_for
from backend.replace_payment import ReplaceError

FEE_SOURCE = "conservative_upper_bound_estimate"
QUOTE_SOURCE = "adapter_fee_quote"
FEE_MULTIPLE = Decimal(2)
FEE_MINIMUM = Decimal("25.00")
FEE_CURRENCIES = frozenset({"EUR", "USD", "GBP"})
FEE_POLICY = "max(2 x live original fee, 25.00 payout-currency units); EUR/USD/GBP only"
FEE_NOTICE = (
    "Fee budget is a conservative upper-bound estimate (conservative_upper_bound_estimate), not a bank "
    "quote or cap; it cannot guarantee future bank pricing. Only the actual fee is charged."
)
_FINAL = frozenset({"FAILED", "CANCELLED"})
_READ_ERRORS = (AirwallexError, TimeoutError, OSError, HistoryError)
_BALANCE_ERRORS = (ValueError, AttributeError, TypeError, *_READ_ERRORS)


def _decimal(value) -> Decimal | None:
    """A finite Decimal, or None when the value is missing or not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _refuse(conn, code: str, incident_id, as_of: str, **detail) -> None:
    """Drop the open transaction (nothing stored), keep the evidence, and stop."""
    conn.rollback()
    detail["code"] = code
    append_audit(
        conn,
        incident_id=incident_id,
        kind="payout_funding_refused",
        detail=json.dumps(detail, sort_keys=True),
        created_at=as_of,
    )
    conn.commit()
    raise ReplaceError(code)


def _live_available(client, currency: str) -> Decimal:
    return cash_available(client.current_balances(), currency)


_NON_CASH = frozenset({"yield", "credit"})  # known account types that are not spendable cash


def cash_available(balances, currency: str) -> Decimal:
    """The one CASH available amount for the currency (documented to include settled conversions).

    YIELD and CREDIT rows are ignored. A row with a missing or unknown account type, a missing
    cash row, or more than one cash row raises ValueError (fail closed)."""
    cash = []
    for row in balances:
        if getattr(row, "currency", None) != currency:
            continue
        kind = str(getattr(row, "account_type", None) or "").strip().lower()
        if kind == "cash":
            cash.append(row)
        elif kind not in _NON_CASH:
            raise ValueError("account type")
    if len(cash) != 1:
        raise ValueError("balance rows")
    rows = cash
    available = _decimal(rows[0].available_amount)
    if available is None or available < 0:
        raise ValueError("available")
    return available


def _fee_estimate(remote, currency: str) -> tuple[Decimal, str] | None:
    """The conservative fallback budget, or None when the original fee or currency is unsupported."""
    if currency not in FEE_CURRENCIES:
        return None
    original = _original_fee(remote, currency)
    if original is None:
        return None
    return max(original[0] * FEE_MULTIPLE, FEE_MINIMUM), original[1]


def _parse_time(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _quoted_fee(client, *, beneficiary_id, amount: Decimal, currency: str, method, as_of: str):
    """(fee, currency) from the optional read-only quote seam, or None when the client has none.

    Raises ValueError when a quote exists but is unreadable, expired, unattributed or for other terms."""
    quoter = getattr(client, "quote_payout_fee", None)
    if quoter is None:
        return None
    try:
        quote = quoter(
            beneficiary_id=beneficiary_id, transfer_amount=amount, transfer_currency=currency, transfer_method=method
        )
        fee = _decimal(quote.fee_amount)
        valid_to = _parse_time(quote.valid_to_at)
        now = _parse_time(as_of)
        if (
            fee is None or fee < 0
            or quote.fee_currency != currency
            or quote.beneficiary_id != beneficiary_id
            or _decimal(quote.transfer_amount) != amount
            or quote.transfer_currency != currency
            or not method or quote.transfer_method != method
            or not getattr(quote, "source", None)
            or valid_to is None or now is None or valid_to <= now
        ):
            raise ValueError("quote")
    except (*_READ_ERRORS, AttributeError, TypeError, ValueError):
        raise ValueError("quote") from None
    return fee, currency


def _original_fee(remote, currency: str) -> tuple[Decimal, str] | None:
    fee = _decimal(remote.fee_amount)
    if fee is None or fee < 0:
        return None
    fee_currency = remote.fee_currency
    if not fee_currency:
        candidates = {c for c in (remote.transfer_currency, remote.source_currency) if c}
        if len(candidates) != 1:
            return None
        fee_currency = candidates.pop()
    if fee_currency != currency:
        return None  # a fee in another currency cannot be compared with this balance
    return fee, fee_currency


def _settle_conversion(conn, client, quote_id: str, incident_id, obligation_id) -> str:
    """The status of the one FX conversion bound to this quote, incident and obligation.

    Only an explicit SETTLED counts. Anything else is refreshed by the stored request id
    (find_conversion only, never a create) and stays pending when the lookup is unavailable."""
    rows = conn.execute(
        "SELECT * FROM execution_intent WHERE kind = 'fx_conversion' AND quote_id = ?", (quote_id,)
    ).fetchall()
    quote = conn.execute("SELECT state, consumed_by FROM fx_quote WHERE quote_id = ?", (quote_id,)).fetchone()
    if len(rows) != 1 or quote is None:
        return "missing"
    conversion = rows[0]
    if (
        conversion["incident_id"] != incident_id
        or conversion["obligation_id"] != obligation_id
        or conversion["state"] != "executed"
        or not conversion["remote_id"]
        or not conversion["request_id"]
        or quote["state"] != "CONSUMED"
        or quote["consumed_by"] != conversion["remote_id"]
    ):
        return "unbound"
    if (conversion["remote_status"] or "").upper() == "SETTLED":
        return "SETTLED"
    finder = getattr(client, "find_conversion", None)
    if finder is None:
        return "unknown"
    try:
        found = finder(conversion["request_id"])
    except _READ_ERRORS:
        return "unknown"
    status = (getattr(found, "status", None) or "").upper()
    if (
        found is None
        or str(found.conversion_id) != conversion["remote_id"]
        or found.quote_id not in (None, quote_id)
        or not status
    ):
        return "unknown"
    conn.execute("UPDATE execution_intent SET remote_status = ? WHERE id = ?", (status, conversion["id"]))
    return status


def gate(
    conn, client, *, obligation, intent_id: int, request_id: str, amount: Decimal, currency: str, as_of: str,
    beneficiary_id: str | None = None, transfer_method: str | None = None,
) -> None:
    """Raise ReplaceError unless this payout is refunded, bound to the live original, and funded.

    Stores the preflight, the before snapshot, and the hold. The caller commits them with the claim."""
    row = conn.execute("SELECT incident_id, quote_id FROM execution_intent WHERE id = ?", (intent_id,)).fetchone()
    incident_id = row["incident_id"] if row is not None else None
    quote_id = row["quote_id"] if row is not None else None
    base = {"request_id": request_id, "intent_id": intent_id, "obligation_id": obligation["id"]}
    record = None
    if obligation["live_transfer_id"] is not None:
        record = conn.execute(
            "SELECT airwallex_id, currency FROM transfer_record WHERE id = ?", (obligation["live_transfer_id"],)
        ).fetchone()
    if record is None or not record["airwallex_id"]:
        _refuse(conn, "original", incident_id, as_of, **base)
    try:
        remote = client.get_transfer(record["airwallex_id"])
    except _READ_ERRORS:
        _refuse(conn, "original", incident_id, as_of, reason="unreadable", **base)
    live = _decimal(remote.transfer_amount)
    if live is None or not remote.transfer_currency or (amount, currency) != (live, remote.transfer_currency):
        _refuse(conn, "void", incident_id, as_of, live_amount=str(remote.transfer_amount), **base)
    if remote.status not in _FINAL:
        _refuse(conn, "original", incident_id, as_of, status=remote.status, **base)
    try:
        reversals = reversals_for(client, record["airwallex_id"], refund_currency(remote, record["currency"]))
    except _READ_ERRORS:
        _refuse(conn, "refund", incident_id, as_of, reason="history", **base)
    if full_refund(remote, reversals) is None:
        _refuse(conn, "refund", incident_id, as_of, **base)
    try:
        quoted = _quoted_fee(
            client, beneficiary_id=beneficiary_id, amount=amount, currency=currency,
            method=transfer_method or remote.transfer_method, as_of=as_of,
        )
    except ValueError:
        _refuse(conn, "fee", incident_id, as_of, reason="quote", **base)
    estimate = quoted or _fee_estimate(remote, currency)
    if estimate is None:
        _refuse(conn, "fee", incident_id, as_of, **base)
    fee, fee_currency = estimate
    fee_source = QUOTE_SOURCE if quoted else FEE_SOURCE
    try:
        floor = reserve_floor()
    except (FundingError, InvalidOperation):
        _refuse(conn, "floor", incident_id, as_of, **base)
    if not floor.is_finite() or floor <= 0:
        _refuse(conn, "floor", incident_id, as_of, **base)
    if quote_id:  # funded by an FX top-up: only an explicitly SETTLED conversion counts
        status = _settle_conversion(conn, client, quote_id, incident_id, obligation["id"])
        if status != "SETTLED":
            _refuse(conn, "conversion", incident_id, as_of, quote_id=quote_id, conversion_status=status, **base)
    try:
        available = _live_available(client, currency)
    except _BALANCE_ERRORS:
        _refuse(conn, "balance", incident_id, as_of, reason="unreadable", **base)
    held = sum(
        (Decimal(r["amount"]) + Decimal(r["fee_budget"]))
        for r in conn.execute(
            "SELECT amount, fee_budget FROM payout_funding "
            "WHERE currency = ? AND hold_state = 'HELD' AND request_id != ?",
            (currency, request_id),
        )
    )
    needed = amount + fee + floor
    if available - held < needed:
        _refuse(
            conn, "balance", incident_id, as_of,
            available=str(available), held=str(held), needed=str(needed), **base,
        )
    conn.execute(
        """
        INSERT INTO payout_funding (
            request_id, intent_id, incident_id, obligation_id, currency, amount, fee_budget, fee_currency,
            fee_source, reserve_floor, available_before, state, hold_state, after_state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREFLIGHT', 'HELD', 'PENDING', ?)
        """,
        (
            request_id, intent_id, incident_id, obligation["id"], currency, format(amount, "f"),
            format(fee, "f"), fee_currency, fee_source, format(floor, "f"), format(available, "f"), as_of,
        ),
    )
    conn.execute(
        "INSERT INTO balance_snapshot (currency, available, taken_at, label, request_id) VALUES (?, ?, ?, 'before', ?)",
        (currency, format(available, "f"), as_of, request_id),
    )
    append_audit(
        conn,
        incident_id=incident_id,
        kind="payout_funding_preflight",
        detail=json.dumps(
            {
                **base, "amount": format(amount, "f"), "currency": currency, "fee_budget": format(fee, "f"),
                "fee_currency": fee_currency, "fee_source": fee_source,
                "fee_policy": "adapter quote" if quoted else FEE_POLICY, "reserve_floor": format(floor, "f"),
                "available_before": format(available, "f"), "held_by_others": format(held, "f"),
                "fee_notice": FEE_NOTICE,
            },
            sort_keys=True,
        ),
        created_at=as_of,
    )


def _row(conn, request_id: str):
    return conn.execute("SELECT * FROM payout_funding WHERE request_id = ?", (request_id,)).fetchone()


def record_acceptance(conn, request_id: str, remote, as_of: str) -> None:
    """The bank accepted the send. Keep the hold, store the actual fee, audit a breach. Caller commits."""
    row = _row(conn, request_id)
    if row is None or row["state"] == "REFUSED":
        return
    conn.execute("UPDATE payout_funding SET state = 'ACCEPTED' WHERE request_id = ?", (request_id,))
    fee = _decimal(remote.fee_amount)
    if fee is None or row["actual_fee"] is not None:
        return
    fee_currency = remote.fee_currency or remote.transfer_currency
    conn.execute(
        "UPDATE payout_funding SET actual_fee = ?, actual_fee_currency = ? WHERE request_id = ?",
        (format(fee, "f"), fee_currency, request_id),
    )
    if fee_currency != row["fee_currency"] or fee > Decimal(row["fee_budget"]):
        append_audit(
            conn,
            incident_id=row["incident_id"],
            kind="payout_fee_over_budget",
            detail=json.dumps(
                {
                    "request_id": request_id, "budget": row["fee_budget"], "budget_currency": row["fee_currency"],
                    "actual": format(fee, "f"), "actual_currency": fee_currency, "action": "escalate_no_resend",
                },
                sort_keys=True,
            ),
            created_at=as_of,
        )
        # Explicit escalation: a person must review. Nothing here ever creates another payout.
        set_obligation_status(conn, row["obligation_id"], "ESCALATED")
        append_audit(
            conn,
            incident_id=row["incident_id"],
            kind="payout_escalated",
            detail=json.dumps({"request_id": request_id, "reason": "fee_over_budget", "resend": False}),
            created_at=as_of,
        )


def release_refused(conn, request_id: str, as_of: str) -> None:
    """A definitive first-send refusal frees its own hold. Caller commits."""
    cursor = conn.execute(
        "UPDATE payout_funding SET state = 'REFUSED', hold_state = 'RELEASED' "
        "WHERE request_id = ? AND state = 'PREFLIGHT'",
        (request_id,),
    )
    row = _row(conn, request_id)
    if cursor.rowcount == 1 and row is not None:
        append_audit(
            conn, incident_id=row["incident_id"], kind="payout_hold_released",
            detail=json.dumps({"request_id": request_id, "reason": "definitive_refusal"}), created_at=as_of,
        )


def observe_after(conn, client, request_id: str, as_of: str) -> None:
    """Read the live balance once for this payout. A failed read is recorded and can be retried. Commits."""
    row = _row(conn, request_id)
    if row is None or row["state"] == "REFUSED" or row["after_state"] == "OBSERVED":
        return
    try:
        available = _live_available(client, row["currency"])
    except _BALANCE_ERRORS:  # an unreadable balance leaves the accepted send intact
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE payout_funding SET after_state = 'MISSING' WHERE request_id = ? AND after_state = 'PENDING'",
            (request_id,),
        )
        if cursor.rowcount == 1:
            append_audit(
                conn, incident_id=row["incident_id"], kind="payout_after_missing",
                detail=json.dumps({"request_id": request_id}), created_at=as_of,
            )
        conn.commit()
        return
    conn.execute("BEGIN IMMEDIATE")
    cursor = conn.execute(
        "UPDATE payout_funding SET after_state = 'OBSERVED' WHERE request_id = ? AND after_state != 'OBSERVED'",
        (request_id,),
    )
    if cursor.rowcount == 1:
        conn.execute(
            "INSERT INTO balance_snapshot (currency, available, taken_at, label, request_id) "
            "VALUES (?, ?, ?, 'after', ?)",
            (row["currency"], format(available, "f"), as_of, request_id),
        )
        append_audit(
            conn, incident_id=row["incident_id"], kind="payout_after_observed",
            detail=json.dumps({"request_id": request_id, "available_after": format(available, "f")}),
            created_at=as_of,
        )
    conn.commit()


def owned_funding(conn, intent_id: int):
    """Read-only. The funding row for this intent, or None when there is none (older send).

    Raises ReplaceError('void') unless the grant, the intent terms, and the funding row all agree.
    Expiry is not checked: an accepted attempt may be recovered after its grant lapsed."""
    from backend.approvals import binding_hash
    from backend.execute import _terms

    row = conn.execute("SELECT * FROM payout_funding WHERE intent_id = ?", (intent_id,)).fetchone()
    if row is None:
        return None
    intent = conn.execute("SELECT * FROM execution_intent WHERE id = ?", (intent_id,)).fetchone()
    approval = None
    if intent is not None and intent["approval_id"] is not None:
        approval = conn.execute("SELECT * FROM approval WHERE id = ?", (intent["approval_id"],)).fetchone()
    if (
        intent is None
        or intent["kind"] != "replacement"
        or intent["state"] != "executed"
        or approval is None
        or approval["incident_id"] != intent["incident_id"]
        or approval["used_at"] is None
        or approval["approver"] == approval["proposer"]
        or binding_hash(_terms(conn, intent_id)) != approval["binding_hash"]
        or row["request_id"] != intent["request_id"]
        or row["obligation_id"] != intent["obligation_id"]
        or row["incident_id"] != intent["incident_id"]
        or row["amount"] != intent["amount"]
        or row["currency"] != intent["currency"]
    ):
        raise ReplaceError("void")
    return row


def backfill_after(conn, client, intent_id: int, as_of: str) -> None:
    """Resume: after validating ownership, give an accepted payout its after observation."""
    row = owned_funding(conn, intent_id)
    if row is not None:
        observe_after(conn, client, row["request_id"], as_of)


def record_recovered(conn, client, intent_id: int, remote, as_of: str) -> None:
    """Reconciliation found our accepted send. Call only after owner and remote terms were validated.

    Stores acceptance durably, then reads the after balance (truthfully observed or missing)."""
    row = owned_funding(conn, intent_id)
    if row is None:
        return
    record_acceptance(conn, row["request_id"], remote, as_of)
    conn.commit()
    observe_after(conn, client, row["request_id"], as_of)


def terms_match(remote, *, request_id: str, invoice_ref: str, amount: Decimal, currency: str, beneficiary_id: str) -> bool:
    """A looked-up transfer is ours only if it carries the exact attempt's request id and terms."""
    remote_amount = _decimal(remote.transfer_amount)
    return (
        remote.request_id == request_id
        and remote.reference == invoice_ref
        and remote.transfer_currency == currency
        and remote_amount is not None
        and remote_amount == amount
        and remote.beneficiary_id == beneficiary_id
    )


def note_for_intent(conn, intent_id: int) -> str | None:
    """What the caller must see: the fee budget is an estimate, not a bank quote."""
    row = conn.execute("SELECT * FROM payout_funding WHERE intent_id = ?", (intent_id,)).fetchone()
    if row is None:
        return None
    return (
        f"Funding checked live: available before {row['available_before']} {row['currency']}, "
        f"reserve {row['reserve_floor']}, fee budget {row['fee_budget']} {row['fee_currency']} ({row['fee_source']}). "
        f"{FEE_NOTICE}"
    )

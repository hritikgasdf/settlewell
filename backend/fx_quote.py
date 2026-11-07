"""One use per quote. Expiry is checked before approval and again before conversion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from backend.airwallex.client import AirwallexError
from backend.approvals import (
    AP_CLERK,
    ApprovalError,
    ApprovalTerms,
    approve,
    binding_hash,
    propose,
)
from backend.fx_funding import conversion_request_id
from backend.ledger import (
    LedgerError,
    consume_fx_quote,
    expire_fx_quote,
    lock_fx_quote,
    record_fx_conversion,
    release_unsent_fx_quote,
)

_AIRWALLEX_EXPIRY = frozenset({"expired_quote", "quote_expired"})


class FxQuoteError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FxDecision:
    outcome: str
    quote_id: str
    approval_id: int
    conversion_id: str | None = None


def propose_fx_quote(conn, incident_id: int, terms: ApprovalTerms, *, proposer: str) -> int:
    """Store an approval whose hash includes this locked quote."""
    if not terms.quote_id:
        raise FxQuoteError("quote")
    row = conn.execute("SELECT state FROM fx_quote WHERE quote_id = ?", (terms.quote_id,)).fetchone()
    if row is None or row["state"] != "LOCKED":
        raise FxQuoteError("quote")
    return propose(conn, incident_id, terms, proposer=proposer)


def approve_fx_quote(
    conn,
    client,
    approval_id: int,
    terms: ApprovalTerms,
    *,
    approver: str,
    used_at: str,
    as_of: str,
) -> FxDecision:
    """Approve a locked quote, or replace it when valid_to has passed."""
    row = _approval(conn, approval_id, terms)
    problem = _usability(conn, terms.quote_id, as_of)
    if problem == "consumed":
        raise FxQuoteError("consumed")
    if problem == "expired":
        return _requote(conn, client, terms, incident_id=row["incident_id"], proposer=row["proposer"])
    approve(conn, approval_id, terms, approver=approver, used_at=used_at)
    return FxDecision("approved", str(terms.quote_id), approval_id)


def execute_fx_quote(conn, client, approval_id: int, terms: ApprovalTerms, *, as_of: str) -> FxDecision:
    """Convert a quote once. A second use is rejected. An expired quote is replaced."""
    row = _approval(conn, approval_id, terms)
    if row["used_at"] is None:
        raise FxQuoteError("unapproved")
    problem = _usability(conn, terms.quote_id, as_of)
    if problem == "consumed":
        raise FxQuoteError("consumed")
    if problem == "expired":
        return _requote(conn, client, terms, incident_id=row["incident_id"], proposer=row["proposer"])
    quote = conn.execute("SELECT * FROM fx_quote WHERE quote_id = ?", (terms.quote_id,)).fetchone()
    sell, buy = quote["pair"].split("/")
    quote_id = str(terms.quote_id)
    from backend.execute import ExecuteError, claim

    try:
        claim(
            conn,
            kind="fx_conversion",
            amount=quote["amount"],
            currency=buy,
            beneficiary_id=terms.beneficiary_id,
            details_hash=terms.details_hash,
            quote_id=quote_id,
            as_of=as_of,
        )
    except ExecuteError as error:
        raise FxQuoteError(error.code) from error
    try:
        consume_fx_quote(conn, quote_id, "")
    except LedgerError:
        raise FxQuoteError("consumed") from None
    conn.commit()
    try:
        conversion = client.create_conversion(
            quote_id=terms.quote_id,
            buy_currency=buy,
            sell_currency=sell,
            buy_amount=Decimal(quote["amount"]),
            request_id=conversion_request_id(quote_id),
        )
    except AirwallexError as error:
        if error.code in _AIRWALLEX_EXPIRY:
            release_unsent_fx_quote(conn, quote_id)
            conn.commit()
            return _requote(conn, client, terms, incident_id=row["incident_id"], proposer=row["proposer"])
        raise
    record_fx_conversion(conn, quote_id, conversion.conversion_id)
    if hasattr(client, "current_balances"):
        from backend.fx_funding import _available, _snapshot

        after = client.current_balances()
        _snapshot(conn, "EUR", _available(after, "EUR"), as_of, "after")
        _snapshot(conn, "USD", _available(after, "USD"), as_of, "after")
    conn.commit()
    return FxDecision("converted", str(terms.quote_id), approval_id, conversion.conversion_id)


def _approval(conn, approval_id: int, terms: ApprovalTerms):
    row = conn.execute("SELECT * FROM approval WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ApprovalError("missing")
    if terms.action != row["action"] or binding_hash(terms) != row["binding_hash"]:
        raise ApprovalError("void")
    return row


def _usability(conn, quote_id: str | None, as_of: str) -> str | None:
    if not quote_id:
        raise FxQuoteError("quote")
    row = conn.execute(
        "SELECT state, valid_to FROM fx_quote WHERE quote_id = ?",
        (quote_id,),
    ).fetchone()
    if row is None:
        raise FxQuoteError("quote")
    if row["state"] == "CONSUMED":
        return "consumed"
    if row["state"] == "EXPIRED" or _at(as_of) >= _at(row["valid_to"]):
        return "expired"
    if row["state"] != "LOCKED":
        raise FxQuoteError("quote")
    return None


def _requote(conn, client, terms: ApprovalTerms, *, incident_id: int, proposer: str | None) -> FxDecision:
    current = conn.execute("SELECT * FROM fx_quote WHERE quote_id = ?", (terms.quote_id,)).fetchone()
    if current is None or current["state"] == "CONSUMED":
        raise FxQuoteError("consumed")
    expire_fx_quote(conn, str(terms.quote_id))
    sell, buy = current["pair"].split("/")
    amount = Decimal(current["amount"])
    quote = client.create_quote(
        buy_currency=buy,
        sell_currency=sell,
        validity="MIN_15",
        buy_amount=amount,
    )
    if not quote.quote_id or not quote.valid_to_at:
        raise FxQuoteError("quote")
    lock_fx_quote(
        conn,
        quote_id=quote.quote_id,
        pair=current["pair"],
        rate=format(quote.client_rate if quote.client_rate is not None else Decimal(0), "f"),
        amount=format(amount, "f"),
        valid_to=quote.valid_to_at,
    )
    new_terms = ApprovalTerms(
        action=terms.action,
        amount=terms.amount,
        currency=terms.currency,
        beneficiary_id=terms.beneficiary_id,
        details_hash=terms.details_hash,
        quote_id=quote.quote_id,
        evidence_hashes=terms.evidence_hashes,
    )
    approval_id = propose(conn, incident_id, new_terms, proposer=proposer or AP_CLERK)
    return FxDecision("reapprove", quote.quote_id, approval_id)


def _at(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and ":" not in text[-5:]:
        text = text[:-2] + ":" + text[-2:]
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed

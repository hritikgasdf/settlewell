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
        return _requote(
            conn,
            client,
            terms,
            incident_id=row["incident_id"],
            proposer=row["proposer"],
            as_of=as_of,
        )
    approve(conn, approval_id, terms, approver=approver, used_at=used_at)
    return FxDecision("approved", str(terms.quote_id), approval_id)


def execute_fx_quote(
    conn,
    client,
    approval_id: int,
    terms: ApprovalTerms,
    *,
    as_of: str,
    intent_id: int | None = None,
) -> FxDecision:
    """Convert a quote once. A second use returns the stored conversion. An expired quote is replaced."""
    row = _approval(conn, approval_id, terms)
    if row["used_at"] is None:
        raise FxQuoteError("unapproved")
    intent = _conversion_intent(conn, str(terms.quote_id), intent_id)
    if intent["remote_id"]:
        return FxDecision("converted", str(terms.quote_id), int(intent["approval_id"]), intent["remote_id"])
    problem = _usability(conn, terms.quote_id, as_of)
    if problem == "consumed":
        raise FxQuoteError("consumed")
    if problem == "expired":
        return _requote(conn, client, terms, incident_id=row["incident_id"], proposer=row["proposer"], as_of=as_of)
    quote = conn.execute("SELECT * FROM fx_quote WHERE quote_id = ?", (terms.quote_id,)).fetchone()
    sell, buy = quote["pair"].split("/")
    quote_id = str(terms.quote_id)
    from backend.execute import ExecuteError, claim, remember_remote

    try:
        claim(
            conn,
            intent_id=int(intent["id"]),
            approval_id=int(intent["approval_id"]),
            obligation_id=int(intent["obligation_id"]),
            as_of=as_of,
            kind="fx_conversion",
            amount=intent["amount"],
            currency=intent["currency"],
            beneficiary_id=intent["beneficiary_id"],
            details_hash=intent["details_hash"],
            quote_id=quote_id,
        )
    except ExecuteError as error:
        raise FxQuoteError(error.code) from error
    conn.commit()
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
            return _requote(
                conn,
                client,
                terms,
                incident_id=row["incident_id"],
                proposer=row["proposer"],
                as_of=as_of,
            )
        raise
    record_fx_conversion(conn, quote_id, conversion.conversion_id)
    remember_remote(conn, int(intent["id"]), conversion.conversion_id, "converted")
    if hasattr(client, "current_balances"):
        from backend.fx_funding import _available, _snapshot

        after = client.current_balances()
        _snapshot(conn, "EUR", _available(after, "EUR"), as_of, "after")
        _snapshot(conn, "USD", _available(after, "USD"), as_of, "after")
    conn.commit()
    return FxDecision("converted", str(terms.quote_id), int(intent["approval_id"]), conversion.conversion_id)


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


def _requote(
    conn,
    client,
    terms: ApprovalTerms,
    *,
    incident_id: int,
    proposer: str | None,
    as_of: str,
) -> FxDecision:
    current = conn.execute("SELECT * FROM fx_quote WHERE quote_id = ?", (terms.quote_id,)).fetchone()
    if current is None or current["state"] == "CONSUMED":
        raise FxQuoteError("consumed")
    previous = _conversion_intent(conn, str(terms.quote_id), None)
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
    from backend.execute import open_intent

    opened = open_intent(
        conn,
        kind="fx_conversion",
        incident_id=int(previous["incident_id"]),
        obligation_id=int(previous["obligation_id"]),
        action=previous["action"],
        amount=previous["amount"],
        currency=previous["currency"],
        beneficiary_id=previous["beneficiary_id"],
        details_hash=previous["details_hash"],
        proposer=proposer or AP_CLERK,
        quote_id=quote.quote_id,
        expires_at=quote.valid_to_at,
        created_at=as_of,
    )
    return FxDecision("reapprove", quote.quote_id, opened.approval_id)


def _conversion_intent(conn, quote_id: str, intent_id: int | None):
    if intent_id is not None:
        row = conn.execute(
            "SELECT * FROM execution_intent WHERE id = ? AND kind = 'fx_conversion'",
            (intent_id,),
        ).fetchone()
        if row is None or (row["quote_id"] or "") != quote_id:
            raise FxQuoteError("approval")
        return row
    rows = conn.execute(
        "SELECT * FROM execution_intent WHERE kind = 'fx_conversion' AND quote_id = ?",
        (quote_id,),
    ).fetchall()
    if len(rows) != 1:
        raise FxQuoteError("approval")
    return rows[0]


def _at(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and ":" not in text[-5:]:
        text = text[:-2] + ":" + text[-2:]
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed

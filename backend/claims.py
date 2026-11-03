"""Strict claim parser. A model may only propose JSON. Code accepts it or escalates."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_REQUIRED = (
    "invoice_ref",
    "amount",
    "currency",
    "asks_where",
    "asks_proof",
    "asks_new_account",
)

HERO_EMAIL = (
    "Lumen Textiles GmbH: EUR 4,200 for INV-2291 never arrived. Pay to our NEW account today."
)


@dataclass(frozen=True)
class Claim:
    invoice_ref: str
    amount: str
    currency: str
    asks_where: bool
    asks_proof: bool
    asks_new_account: bool


@dataclass(frozen=True)
class ParseResult:
    escalated: bool
    rule_id: str
    claim: Claim | None = None
    obligation_id: int | None = None
    transfer_id: int | None = None


def parse_email(email: str, complete: Callable[[str], str], conn) -> ParseResult:
    """Ask the model for JSON, then match a valid claim to the ledger."""
    if not email.strip():
        return ParseResult(True, "R-EMPTY-EMAIL")
    claim = _claim_from_json(complete(email))
    if claim is None:
        return ParseResult(True, "R-BAD-JSON")
    return _match(conn, claim)


def _claim_from_json(raw: str) -> Claim | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or set(payload) != set(_REQUIRED):
        return None
    invoice_ref = payload["invoice_ref"]
    amount = payload["amount"]
    currency = payload["currency"]
    flags = (payload["asks_where"], payload["asks_proof"], payload["asks_new_account"])
    if not isinstance(invoice_ref, str) or not invoice_ref.strip():
        return None
    if not isinstance(amount, str) or not isinstance(currency, str):
        return None
    if len(currency) != 3 or not currency.isalpha():
        return None
    if not all(isinstance(flag, bool) for flag in flags):
        return None
    try:
        Decimal(amount)
    except InvalidOperation:
        return None
    return Claim(
        invoice_ref=invoice_ref.strip(),
        amount=format(Decimal(amount), "f"),
        currency=currency.upper(),
        asks_where=flags[0],
        asks_proof=flags[1],
        asks_new_account=flags[2],
    )


def _match(conn, claim: Claim) -> ParseResult:
    row = conn.execute(
        """
        SELECT obligation.id AS obligation_id, obligation.amount, obligation.currency,
               transfer_record.id AS transfer_id
        FROM obligation
        JOIN transfer_record ON transfer_record.id = obligation.live_transfer_id
        WHERE obligation.invoice_ref = ?
        """,
        (claim.invoice_ref,),
    ).fetchone()
    if row is None:
        return ParseResult(True, "R-NO-MATCH", claim)
    if Decimal(row["amount"]) != Decimal(claim.amount) or row["currency"] != claim.currency:
        return ParseResult(True, "R-AMOUNT-MISMATCH", claim)
    return ParseResult(
        False,
        "R-MATCHED",
        claim,
        int(row["obligation_id"]),
        int(row["transfer_id"]),
    )

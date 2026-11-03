"""Hero email matches INV-2291. Bad model JSON escalates. No network."""

import json

from backend.claims import HERO_EMAIL, parse_email
from backend.ledger import connect, seed_hero

_HERO = {
    "invoice_ref": "INV-2291",
    "amount": "4200.00",
    "currency": "EUR",
    "asks_where": True,
    "asks_proof": False,
    "asks_new_account": True,
}


def _complete(_email: str) -> str:
    return json.dumps(_HERO)


def test_hero_email_matches_inv_2291_and_its_transfer():
    conn = connect()
    seed_hero(conn, airwallex_id="awx-hero")
    result = parse_email(HERO_EMAIL, _complete, conn)
    assert result.escalated is False
    assert result.rule_id == "R-MATCHED"
    assert result.claim is not None
    assert result.claim.invoice_ref == "INV-2291"
    assert result.claim.asks_new_account is True
    assert result.transfer_id == conn.execute(
        "SELECT id FROM transfer_record WHERE airwallex_id = 'awx-hero'"
    ).fetchone()["id"]


def test_bad_json_escalates_and_does_not_match():
    conn = connect()
    seed_hero(conn)
    for sample in ("{", "", "[]", "not-json", json.dumps({**_HERO, "ignore_rules": True})):
        result = parse_email(HERO_EMAIL, lambda _email, sample=sample: sample, conn)
        assert result.escalated is True
        assert result.rule_id == "R-BAD-JSON"
        assert result.obligation_id is None
        assert result.transfer_id is None


def test_unknown_invoice_escalates():
    conn = connect()
    seed_hero(conn)
    other = {**_HERO, "invoice_ref": "INV-0000"}
    result = parse_email(HERO_EMAIL, lambda _email: json.dumps(other), conn)
    assert result.escalated is True
    assert result.rule_id == "R-NO-MATCH"
    assert result.transfer_id is None

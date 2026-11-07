"""Hero email matches INV-2291. Bad model JSON escalates. Pinned identity decides. No network."""

import json

from backend.claims import HERO_EMAIL, mock_complete, parse_email
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


def _pinned(conn) -> int:
    return int(conn.execute("SELECT supplier_id FROM obligation").fetchone()["supplier_id"])


def test_hero_email_matches_inv_2291_and_its_transfer():
    conn = connect()
    seed_hero(conn, airwallex_id="awx-hero")
    result = parse_email(HERO_EMAIL, _complete, conn, supplier_id=_pinned(conn))
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
    bad = (
        "{",
        "",
        "[]",
        "not-json",
        json.dumps({**_HERO, "ignore_rules": True}),
        json.dumps({**_HERO, "amount": "NaN"}),
        json.dumps({**_HERO, "amount": "Infinity"}),
        json.dumps({**_HERO, "amount": "-1"}),
        json.dumps({**_HERO, "amount": 4200}),
        json.dumps({**_HERO, "invoice_ref": "X" * 200}),
    )
    for sample in bad:
        result = parse_email(
            HERO_EMAIL, lambda _e, sample=sample: sample, conn, supplier_id=_pinned(conn)
        )
        assert result.escalated is True
        assert result.rule_id == "R-BAD-JSON"
        assert result.obligation_id is None
        assert result.transfer_id is None


def test_unknown_invoice_escalates():
    conn = connect()
    seed_hero(conn)
    other = {**_HERO, "invoice_ref": "INV-0000"}
    result = parse_email(
        HERO_EMAIL, lambda _email: json.dumps(other), conn, supplier_id=_pinned(conn)
    )
    assert result.escalated is True
    assert result.rule_id == "R-NO-MATCH"
    assert result.transfer_id is None


def test_mock_complete_is_deterministic_and_matches_hero():
    conn = connect()
    seed_hero(conn)
    assert mock_complete(HERO_EMAIL) == mock_complete(HERO_EMAIL)
    result = parse_email(HERO_EMAIL, mock_complete, conn, supplier_id=_pinned(conn))
    assert result.rule_id == "R-MATCHED"

"""Opt-in live Claude check: SETTLEWELL_LIVE_CLAUDE=1 and ANTHROPIC_API_KEY. Synthetic data only."""

import json
import os
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from backend import claims
from backend.ledger import connect, seed_hero

_PROOF = Path("/workspace/settlewell-ctx/p14-live-proof.json")
_EMAIL = (
    "Hello, this is Lumen Textiles GmbH accounts. Invoice INV-2291 for EUR 4,200.00 has not "
    "arrived. Where is the payment? Please send it to our NEW bank account instead."
)

pytestmark = pytest.mark.live_claude


@pytest.fixture(autouse=True)
def _allow_network_when_opted_in(monkeypatch: pytest.MonkeyPatch):
    # Runs after the suite-wide socket block fixture; restore real sockets for this opt-in test.
    if os.environ.get("SETTLEWELL_LIVE_CLAUDE") == "1":
        monkeypatch.setattr(socket, "socket", _REAL_SOCKET)
        monkeypatch.setattr(socket, "create_connection", _REAL_CREATE)


_REAL_SOCKET = socket.socket
_REAL_CREATE = socket.create_connection


@pytest.mark.skipif(
    os.environ.get("SETTLEWELL_LIVE_CLAUDE") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="set SETTLEWELL_LIVE_CLAUDE=1 and ANTHROPIC_API_KEY to run",
)
def test_live_claude_extracts_synthetic_claim_with_pinned_identity(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "0")
    conn = connect()
    seed_hero(conn)
    supplier_id = int(conn.execute("SELECT supplier_id FROM obligation").fetchone()["supplier_id"])
    other = conn.execute("INSERT INTO supplier (name) VALUES ('Synthetic Other Co')")
    conn.commit()

    text, meta = claims.complete_with_meta(_EMAIL)
    claim = claims._claim_from_json(text)
    assert claim is not None, "strict validator rejected live output"
    result = claims.parse_email(_EMAIL, lambda _e: text, conn, supplier_id=supplier_id)
    spoof = claims.parse_email(_EMAIL, lambda _e: text, conn, supplier_id=int(other.lastrowid))

    assert result.rule_id == "R-MATCHED" and result.escalated is False
    assert spoof.rule_id == "R-IDENTITY-MISMATCH" and spoof.escalated is True
    assert claim.invoice_ref == "INV-2291"
    assert Decimal(claim.amount) == Decimal(4200) and claim.currency == "EUR"
    assert claim.asks_where is True and claim.asks_new_account is True

    proof = {
        "model_used": meta["model"],
        "input_tokens": meta["input_tokens"],
        "output_tokens": meta["output_tokens"],
        "parsed": {
            "invoice_ref": "<redacted>",
            "amount": claim.amount,
            "currency": claim.currency,
            "asks_where": claim.asks_where,
            "asks_proof": claim.asks_proof,
            "asks_new_account": claim.asks_new_account,
        },
        "rule_id": result.rule_id,
        "spoof_rule_id": spoof.rule_id,
    }
    print(json.dumps(proof, sort_keys=True))
    try:
        _PROOF.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    except OSError:
        print("proof file not written")

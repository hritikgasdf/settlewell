"""Proof is attached while SENT and refused after CANCELLED. No network."""

import hashlib

from backend.airwallex.types import ConfirmationLetter, Transfer
from backend.ledger import connect, seed_hero
from backend.proof import CUTOFF_LABEL, ETA_LABEL, issue_proof


class Letters:
    def __init__(self, status: str, **extra) -> None:
        self.status = status
        self.extra = extra
        self.calls: list[dict] = []

    def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer(
            id=transfer_id,
            status=self.status,
            transfer_currency="EUR",
            transfer_method="SWIFT",
            short_reference_id="P261103-TEST",
            transfer_date="2026-11-03",
            dispatch_date="2026-11-04",
            **self.extra,
        )

    def create_confirmation_letter(self, **kwargs) -> ConfirmationLetter:
        self.calls.append(kwargs)
        return ConfirmationLetter(content=b"%PDF-1.4 proof", media_type="application/pdf")


def _seed():
    conn = connect()
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    transfer_id = conn.execute(
        "SELECT id FROM transfer_record WHERE airwallex_id = 'awx-hero'"
    ).fetchone()["id"]
    return conn, incident_id, transfer_id


def test_proof_is_attached_while_sent_and_hashed_in_the_audit():
    conn, incident_id, transfer_id = _seed()
    client = Letters("SENT")
    result = issue_proof(conn, client, transfer_id, incident_id=incident_id, seen_at="2026-11-03T12:00:00+00:00")
    digest = hashlib.sha256(b"%PDF-1.4 proof").hexdigest()

    assert result.attached is True
    assert result.sha256 == digest
    assert result.rule_id == "R-PROOF-SENT"
    assert client.calls == [{"transaction_id": "awx-hero", "letter_format": "NO_FEE_DISPLAY"}]
    assert ETA_LABEL in result.eta
    assert "1–3 business days" in result.eta
    assert result.cutoff_label == CUTOFF_LABEL
    assert result.trace_draft is not None
    assert "not retrieved from the bank network" in result.trace_draft
    assert "P261103-TEST" in result.trace_draft
    stored = conn.execute("SELECT format, status_at_issue, sha256, sent_at FROM proof_document").fetchone()
    assert stored["format"] == "NO_FEE_DISPLAY"
    assert stored["status_at_issue"] == "SENT"
    assert stored["sha256"] == digest
    assert stored["sent_at"] is None
    audit = conn.execute("SELECT kind, detail FROM audit_event").fetchone()
    assert audit["kind"] == "proof_attached"
    assert digest in audit["detail"]


def test_proof_is_refused_after_cancelled_and_no_letter_is_fetched():
    conn, incident_id, transfer_id = _seed()
    client = Letters("CANCELLED", failure_reason="90701: Account closed")
    result = issue_proof(conn, client, transfer_id, incident_id=incident_id, seen_at="2026-11-03T12:01:00+00:00")

    assert result.attached is False
    assert result.sha256 is None
    assert client.calls == []
    assert "cancelled" in result.reply_draft.lower()
    assert conn.execute("SELECT COUNT(*) AS n FROM proof_document").fetchone()["n"] == 0
    audit = conn.execute("SELECT kind, detail FROM audit_event").fetchone()
    assert audit["kind"] == "proof_refused"
    assert "CANCELLED" in audit["detail"]


def test_wait_reply_while_processing_does_not_fetch_a_letter():
    conn, incident_id, transfer_id = _seed()
    client = Letters("PROCESSING")
    result = issue_proof(conn, client, transfer_id, incident_id=incident_id, seen_at="2026-11-03T12:02:00+00:00")
    assert result.attached is False
    assert client.calls == []
    assert "still on its way" in result.reply_draft
    assert ETA_LABEL in result.reply_draft


def test_bank_letter_bytes_persist_atomically_and_serve_without_mock_seed():
    from backend import api_proof

    conn, incident_id, transfer_id = _seed()
    conn.execute("DROP TABLE proof_blob")  # legacy DB created before the table existed
    client = Letters("PAID")
    issue_proof(conn, client, transfer_id, incident_id=incident_id, seen_at="2026-11-03T12:00:00+00:00")
    doc = conn.execute("SELECT * FROM proof_document").fetchone()
    blob = conn.execute("SELECT * FROM proof_blob").fetchone()
    assert blob["content"] == b"%PDF-1.4 proof"
    assert blob["sha256"] == doc["sha256"] == hashlib.sha256(b"%PDF-1.4 proof").hexdigest()
    assert blob["origin"] == "airwallex_confirmation_letter"
    assert "MOCK" not in blob["label"] and "SYNTHETIC" not in blob["label"]
    ob_id = conn.execute("SELECT obligation_id FROM transfer_record WHERE id = ?", (transfer_id,)).fetchone()[0]
    conn.execute("UPDATE obligation SET live_transfer_id = ? WHERE id = ?", (transfer_id, ob_id))
    conn.execute("UPDATE transfer_record SET status = 'PAID' WHERE id = ?", (transfer_id,))
    data, headers = api_proof.load_pdf(conn, lambda c: client, incident_id, doc["id"])
    assert data == b"%PDF-1.4 proof"
    assert headers["X-Settlewell-Proof-Origin"] == "airwallex_confirmation_letter"
    conn.execute("UPDATE proof_blob SET sha256 = 'bad'")
    try:
        api_proof.load_pdf(conn, lambda c: client, incident_id, doc["id"])
        raise AssertionError("tampered hash must refuse")
    except LookupError as exc:
        assert exc.args[1] == 409


def test_cancelled_never_downloads_and_missing_blob_is_graceful():
    from backend import api_proof

    conn, incident_id, transfer_id = _seed()
    client = Letters("CANCELLED")
    result = issue_proof(conn, client, transfer_id, incident_id=incident_id, seen_at="2026-11-03T12:00:00+00:00")
    assert result.attached is False and client.calls == []
    assert conn.execute("SELECT COUNT(*) FROM proof_blob").fetchone()[0] == 0
    conn.execute("DROP TABLE proof_blob")
    try:
        api_proof.load_pdf(conn, lambda c: client, incident_id, 999)
    except LookupError as exc:
        assert exc.args[1] == 404

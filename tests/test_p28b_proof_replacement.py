"""A PAID replacement's proof is served while the obligation's original anchor stays unchanged."""
from test_proof import Letters, _seed

from backend import api_proof
from backend.proof import issue_proof

SEEN = "2026-11-03T12:00:00+00:00"
INTENT_SQL = (
    "INSERT INTO execution_intent (kind, incident_id, obligation_id, action, amount, currency, beneficiary_id, "
    "details_hash, state, created_at, remote_id, request_id) VALUES ('replacement', ?, ?, 'REPLACE', '1', 'EUR', "
    "'b', 'h', ?, 't', ?, ?)")


def _world(origin="settlewell", remote="awx-repl", intent_state="executed"):
    conn, incident_id, anchor_id = _seed()
    ob_id = conn.execute("SELECT obligation_id FROM transfer_record WHERE id = ?", (anchor_id,)).fetchone()[0]
    conn.execute("UPDATE obligation SET live_transfer_id = ? WHERE id = ?", (anchor_id, ob_id))
    conn.execute("UPDATE transfer_record SET status = 'CANCELLED' WHERE id = ?", (anchor_id,))
    rec_id = conn.execute(
        "INSERT INTO transfer_record (airwallex_id, request_id, obligation_id, origin, status) "
        "VALUES ('awx-repl', 'stl-r-1', ?, ?, 'PAID')", (ob_id, origin)).lastrowid
    conn.execute(INTENT_SQL, (incident_id, ob_id, intent_state, remote, "stl-r-1"))
    client = Letters("PAID")
    issue_proof(conn, client, rec_id, incident_id=incident_id, seen_at=SEEN)
    doc = conn.execute("SELECT * FROM proof_document WHERE transfer_record_id = ?", (rec_id,)).fetchone()
    return conn, client, incident_id, doc, ob_id, anchor_id


def _refusal(conn, client, incident_id, doc):
    try:
        api_proof.load_pdf(conn, lambda c: client, incident_id, doc["id"])
    except LookupError as exc:
        return exc.args[0]
    raise AssertionError("must refuse")


def test_paid_replacement_proof_is_served_with_original_anchor_unchanged():
    conn, client, incident_id, doc, ob_id, anchor_id = _world()
    data, _ = api_proof.load_pdf(conn, lambda c: client, incident_id, doc["id"])
    assert data.startswith(b"%PDF-")
    row = conn.execute("SELECT live_transfer_id FROM obligation WHERE id = ?", (ob_id,)).fetchone()
    assert row[0] == anchor_id


def test_foreign_unowned_or_unexecuted_replacement_is_refused():
    for kwargs in ({"origin": "external"}, {"remote": "awx-other"}, {"intent_state": "proposed"}):
        conn, client, incident_id, doc, *_ = _world(**kwargs)
        assert _refusal(conn, client, incident_id, doc) == "proof_not_eligible:not_live_transfer"


def test_obsolete_replacement_superseded_by_a_newer_intent_is_refused():
    conn, client, incident_id, doc, ob_id, _ = _world()
    conn.execute(INTENT_SQL, (incident_id, ob_id, "executed", "awx-newer", "stl-r-2"))
    assert _refusal(conn, client, incident_id, doc) == "proof_not_eligible:not_live_transfer"


def test_cancelled_original_is_never_eligible_even_as_anchor():
    conn, client, incident_id, _doc, _ob_id, anchor_id = _world()
    issue_proof(conn, Letters("SENT"), anchor_id, incident_id=incident_id, seen_at=SEEN)
    doc = conn.execute("SELECT * FROM proof_document WHERE transfer_record_id = ?", (anchor_id,)).fetchone()
    assert _refusal(conn, client, incident_id, doc) == "proof_not_eligible:status_not_eligible"

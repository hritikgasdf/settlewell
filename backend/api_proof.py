"""Stored-proof PDF serving and the offline seeded claim for the local demo API.

Only bytes that are already persisted in ``proof_blob`` are served, selected by integer ids.
Nothing is read from the filesystem or from a claim. Reads never write. Seeded rows are
labelled MOCK / synthetic and never pretend to be Airwallex proof.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict

from backend.claims import HERO_EMAIL, mock_complete, parse_email
from backend.ledger import PROOF_BLOB  # noqa: F401 - schema re-export
from backend.policy import decide
from backend.webhooks import obligation_pending

MOCK_FORMAT = "MOCK_SYNTHETIC"
MOCK_LABEL = "MOCK / SYNTHETIC - not Airwallex proof, no bank or network confirmation"
PDF_MEDIA_TYPE = "application/pdf"



def pdf_url(incident_id: int, document_id: int) -> str:
    return f"/api/incidents/{incident_id}/proof/{document_id}/pdf"


def build_mock_pdf(lines: list[str]) -> bytes:
    """A small but structurally valid one-page PDF (xref offsets are exact)."""
    safe = [ln.encode("ascii", "replace").decode().replace("\\", "\\\\").replace("(", "[").replace(")", "]")
            for ln in lines]
    text = "BT /F1 12 Tf 50 750 Td 16 TL " + " ".join(f"({ln}) Tj T*" for ln in safe) + " ET"
    stream = text.encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
         b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def seed_claim(conn, incident_id: int, supplier_id: int) -> bool:
    """Run the real offline parser on the synthetic hero email with the pinned supplier.

    The supplier id comes from our obligation, never from the email. The mock completer is
    passed explicitly, so there is no model call. Only a matched claim is persisted, and only
    onto an incident that has no claim yet."""
    result = parse_email(HERO_EMAIL, mock_complete, conn, supplier_id=supplier_id)
    if result.escalated or result.claim is None:
        return False
    payload = {**asdict(result.claim), "rule_id": result.rule_id, "matched": True,
               "obligation_id": result.obligation_id, "transfer_id": result.transfer_id,
               "pinned_supplier_id": supplier_id, "source": "offline_mock_parser_synthetic_email"}
    conn.execute("UPDATE incident SET claim_json = ? WHERE id = ? AND claim_json IS NULL",
                 (json.dumps(payload, sort_keys=True), incident_id))
    return True


def seed_mock_proof(conn, transfer_record_id: int, invoice_ref: str, status: str) -> int:
    content = build_mock_pdf([
        MOCK_LABEL, f"Synthetic transfer notice for {invoice_ref}", f"Status at issue: {status}",
        "Generated locally for the demo. Not a bank document.",
    ])
    digest = hashlib.sha256(content).hexdigest()
    cur = conn.execute(
        """INSERT INTO proof_document (transfer_record_id, format, status_at_issue, sha256, sent_at)
        VALUES (?, ?, ?, ?, NULL)""", (transfer_record_id, MOCK_FORMAT, status, digest))
    conn.execute(
        """INSERT INTO proof_blob (proof_document_id, content, sha256, origin, label)
        VALUES (?, ?, ?, 'mock_synthetic', ?)""", (cur.lastrowid, content, digest, MOCK_LABEL))
    return int(cur.lastrowid)


def _allowed(status: str | None) -> bool:
    return bool(status) and decide(str(status), claim="proof").proof_allowed


def _current_replacement(conn, obligation_id: int, record_id: int) -> bool:
    """True only for the record produced by this obligation's LATEST executed replacement intent.

    The obligation's live_transfer_id stays the immutable original anchor; ownership is proved by the
    durable intent (same obligation, executed, same remote id and request id), never by the record alone."""
    intent = conn.execute(
        "SELECT remote_id, request_id FROM execution_intent WHERE kind = 'replacement' AND state = 'executed' "
        "AND obligation_id = ? AND remote_id IS NOT NULL ORDER BY id DESC LIMIT 1", (obligation_id,)).fetchone()
    record = conn.execute("SELECT * FROM transfer_record WHERE id = ?", (record_id,)).fetchone()
    if intent is None or record is None or record["obligation_id"] != obligation_id:
        return False
    if record["origin"] != "settlewell" or not record["airwallex_id"] or record["airwallex_id"] != intent["remote_id"]:
        return False
    return not (intent["request_id"] and record["request_id"] and intent["request_id"] != record["request_id"])


def eligibility(conn, make_client, doc, obligation) -> str | None:
    """None when the stored proof may be shown now, else a short refusal reason. Never writes."""
    if doc["transfer_record_id"] != obligation["live_transfer_id"] and not _current_replacement(
            conn, obligation["id"], doc["transfer_record_id"]):
        return "not_live_transfer"
    if obligation_pending(conn, obligation["id"]):
        return "webhook_pending"
    record = conn.execute("SELECT * FROM transfer_record WHERE id = ?",
                          (doc["transfer_record_id"],)).fetchone()
    if record is None or not _allowed(record["status"]) or not _allowed(doc["status_at_issue"]):
        return "status_not_eligible"
    snap = conn.execute("SELECT status FROM status_snapshot WHERE transfer_record_id = ? "
                        "ORDER BY id DESC LIMIT 1", (record["id"],)).fetchone()
    if snap is not None and not _allowed(snap["status"]):
        return "status_not_eligible"
    try:
        remote = make_client(conn).get_transfer(record["airwallex_id"])
    except Exception:  # noqa: BLE001 - unknown remote state fails closed
        return "remote_unknown"
    if not _allowed(getattr(remote, "status", None)):
        return "status_not_eligible"
    return None


def _blob_ok(conn, doc) -> bool:
    try:
        blob = conn.execute("SELECT content, sha256 FROM proof_blob WHERE proof_document_id = ?",
                            (doc["id"],)).fetchone()
    except sqlite3.OperationalError:
        return False
    if blob is None:
        return False
    return (hashlib.sha256(blob["content"]).hexdigest() == blob["sha256"] == doc["sha256"]
            and blob["content"][:5] == b"%PDF-")


def proof_view(conn, make_client, incident_id: int) -> dict:
    inc = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (incident_id,)).fetchone()
    ob = conn.execute("SELECT * FROM obligation WHERE id = ?", (inc["obligation_id"],)).fetchone()
    docs = conn.execute(
        """SELECT d.*, b.origin AS blob_origin, b.label AS blob_label,
           (b.proof_document_id IS NOT NULL) AS has_bytes FROM proof_document d
           JOIN transfer_record t ON t.id = d.transfer_record_id AND t.obligation_id = ?
           LEFT JOIN proof_blob b ON b.proof_document_id = d.id ORDER BY d.id""",
        (ob["id"],)).fetchall()
    out, link, reason = [], None, "no stored proof document is currently eligible"
    for doc in docs:
        refusal = ("no_stored_bytes" if not doc["has_bytes"]
                   else "hash_mismatch" if not _blob_ok(conn, doc)
                   else eligibility(conn, make_client, doc, ob))
        url = pdf_url(incident_id, doc["id"]) if refusal is None else None
        link = link or url
        out.append({"id": doc["id"], "transfer_record_id": doc["transfer_record_id"],
                    "format": doc["format"], "status_at_issue": doc["status_at_issue"],
                    "sha256": doc["sha256"], "sent_at": doc["sent_at"], "pdf_url": url,
                    "refusal": refusal, "origin": doc["blob_origin"], "label": doc["blob_label"]})
    if link:
        reason = None
    return {"pdf_link": link, "reason": reason, "documents": out}


def load_pdf(conn, make_client, incident_id: int, document_id: int) -> tuple[bytes, dict]:
    """Return (bytes, headers) or raise LookupError(code, http_status)."""
    inc = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (incident_id,)).fetchone()
    doc = conn.execute("SELECT * FROM proof_document WHERE id = ?", (document_id,)).fetchone()
    if inc is None or doc is None:
        raise LookupError("not_found", 404)
    ob = conn.execute("SELECT * FROM obligation WHERE id = ?", (inc["obligation_id"],)).fetchone()
    owner = conn.execute("SELECT obligation_id FROM transfer_record WHERE id = ?",
                         (doc["transfer_record_id"],)).fetchone()
    if owner is None or owner["obligation_id"] != ob["id"]:
        raise LookupError("not_found", 404)
    try:
        blob = conn.execute("SELECT * FROM proof_blob WHERE proof_document_id = ?", (doc["id"],)).fetchone()
    except sqlite3.OperationalError:
        blob = None
    if blob is None:
        raise LookupError("proof_unavailable", 404)
    if not _blob_ok(conn, doc) or blob["media_type"] != PDF_MEDIA_TYPE:
        raise LookupError("proof_integrity", 409)
    refusal = eligibility(conn, make_client, doc, ob)
    if refusal is not None:
        raise LookupError("proof_not_eligible:" + refusal, 409)
    headers = {"Content-Disposition": f'inline; filename="proof-{doc["id"]}.pdf"',
               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "X-Settlewell-Proof-Origin": blob["origin"]}
    return bytes(blob["content"]), headers

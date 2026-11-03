"""SQLite ledger. Versions, snapshots, and the audit log are append-only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT,
    phone_on_file TEXT
);
CREATE TABLE IF NOT EXISTS bank_detail_version (
    id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    beneficiary_id TEXT,
    details_hash TEXT NOT NULL,
    bank_name TEXT,
    account_last4 TEXT,
    source TEXT NOT NULL CHECK (source IN ('onboarding', 'email_claim', 'verified_callback')),
    verified_by TEXT,
    verified_at TEXT
);
CREATE TABLE IF NOT EXISTS obligation (
    id INTEGER PRIMARY KEY,
    invoice_ref TEXT NOT NULL UNIQUE,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'IN_FLIGHT', 'PAID_ONCE', 'ESCALATED')),
    live_transfer_id INTEGER
);
CREATE TABLE IF NOT EXISTS transfer_record (
    id INTEGER PRIMARY KEY,
    airwallex_id TEXT UNIQUE,
    request_id TEXT,
    short_reference_id TEXT,
    obligation_id INTEGER REFERENCES obligation(id),
    origin TEXT NOT NULL CHECK (origin IN ('settlewell', 'external')),
    details_hash TEXT,
    amount TEXT,
    currency TEXT,
    fee TEXT,
    status TEXT,
    failure_code TEXT,
    failure_message TEXT,
    dispatch_date TEXT,
    reference TEXT
);
CREATE TABLE IF NOT EXISTS status_snapshot (
    id INTEGER PRIMARY KEY,
    transfer_record_id INTEGER NOT NULL REFERENCES transfer_record(id),
    status TEXT NOT NULL,
    failure_code TEXT,
    failure_message TEXT,
    source TEXT NOT NULL CHECK (source IN ('poll', 'webhook', 'transition')),
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incident (
    id INTEGER PRIMARY KEY,
    obligation_id INTEGER NOT NULL REFERENCES obligation(id),
    claim_json TEXT,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incident(id),
    rule_id TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    proposed_action TEXT NOT NULL,
    policy_verdict TEXT NOT NULL,
    llm_rationale TEXT
);
CREATE TABLE IF NOT EXISTS approval (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incident(id),
    action TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    expires_at TEXT,
    used_at TEXT,
    approver TEXT,
    proposer TEXT
);
CREATE TABLE IF NOT EXISTS proof_document (
    id INTEGER PRIMARY KEY,
    transfer_record_id INTEGER NOT NULL REFERENCES transfer_record(id),
    format TEXT NOT NULL,
    status_at_issue TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS webhook_event (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    x_timestamp TEXT NOT NULL,
    signature_ok INTEGER NOT NULL,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS fx_quote (
    id INTEGER PRIMARY KEY,
    quote_id TEXT NOT NULL UNIQUE,
    pair TEXT NOT NULL,
    rate TEXT NOT NULL,
    amount TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('LOCKED', 'CONSUMED', 'EXPIRED')),
    consumed_by TEXT
);
CREATE TABLE IF NOT EXISTS balance_snapshot (
    id INTEGER PRIMARY KEY,
    currency TEXT NOT NULL,
    available TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_case (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    fixture_json TEXT NOT NULL,
    expected_decision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_run (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES eval_case(id),
    invariant_results TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_message (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incident(id),
    approval_id INTEGER NOT NULL REFERENCES approval(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

HERO_SUPPLIER = "Lumen Textiles GmbH"
HERO_INVOICE = "INV-2291"
HERO_AMOUNT = "4200.00"
HERO_CURRENCY = "EUR"
MODEL_TABLES = frozenset(
    {
        "supplier",
        "bank_detail_version",
        "obligation",
        "transfer_record",
        "status_snapshot",
        "incident",
        "decision",
        "approval",
        "proof_document",
        "webhook_event",
        "fx_quote",
        "balance_snapshot",
        "audit_event",
        "eval_case",
        "eval_run",
        "outbox_message",
    }
)


class LedgerError(Exception):
    """A ledger rule rejected the write."""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(approval)")}
    if "proposer" not in columns:
        conn.execute("ALTER TABLE approval ADD COLUMN proposer TEXT")
    conn.commit()


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def seed_hero(conn: sqlite3.Connection, *, airwallex_id: str = "synthetic-inv-2291") -> int:
    """Create Lumen Textiles, INV-2291, and its one live transfer. Returns the incident id."""
    migrate(conn)
    existing = conn.execute(
        "SELECT id FROM obligation WHERE invoice_ref = ?",
        (HERO_INVOICE,),
    ).fetchone()
    if existing:
        incident = conn.execute(
            "SELECT id FROM incident WHERE obligation_id = ?",
            (existing["id"],),
        ).fetchone()
        return int(incident["id"])

    supplier = conn.execute(
        "INSERT INTO supplier (name, contact, phone_on_file) VALUES (?, ?, ?)",
        (HERO_SUPPLIER, "ap@lumen.example", "+49-30-000000"),
    )
    append_bank_detail_version(
        conn,
        supplier_id=int(supplier.lastrowid),
        beneficiary_id=None,
        details_hash="onboarding-v1",
        bank_name="Commerzbank",
        account_last4="4400",
        source="onboarding",
    )
    obligation = conn.execute(
        """
        INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status)
        VALUES (?, ?, ?, ?, 'OPEN')
        """,
        (HERO_INVOICE, HERO_AMOUNT, HERO_CURRENCY, supplier.lastrowid),
    )
    transfer = conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, obligation_id, origin, details_hash,
            amount, currency, status, reference
        ) VALUES (?, ?, ?, 'settlewell', ?, ?, ?, 'SENT', ?)
        """,
        (
            airwallex_id,
            "stl-inv2291-1",
            obligation.lastrowid,
            "onboarding-v1",
            HERO_AMOUNT,
            HERO_CURRENCY,
            HERO_INVOICE,
        ),
    )
    conn.execute(
        "UPDATE obligation SET live_transfer_id = ? WHERE id = ?",
        (transfer.lastrowid, obligation.lastrowid),
    )
    incident = conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, NULL, 'OPEN')",
        (obligation.lastrowid,),
    )
    conn.commit()
    return int(incident.lastrowid)


def hero_bundle(conn: sqlite3.Connection, incident_id: int) -> dict:
    incident = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
    obligation = conn.execute(
        "SELECT * FROM obligation WHERE id = ?",
        (incident["obligation_id"],),
    ).fetchone()
    supplier = conn.execute("SELECT * FROM supplier WHERE id = ?", (obligation["supplier_id"],)).fetchone()
    transfers = conn.execute(
        "SELECT * FROM transfer_record WHERE obligation_id = ? ORDER BY id",
        (obligation["id"],),
    ).fetchall()
    versions = conn.execute(
        "SELECT * FROM bank_detail_version WHERE supplier_id = ? ORDER BY id",
        (supplier["id"],),
    ).fetchall()
    return {
        "incident": incident,
        "obligation": obligation,
        "supplier": supplier,
        "transfers": transfers,
        "versions": versions,
    }


def append_bank_detail_version(
    conn: sqlite3.Connection,
    *,
    supplier_id: int,
    beneficiary_id: str | None,
    details_hash: str,
    bank_name: str,
    account_last4: str,
    source: str,
    verified_by: str | None = None,
    verified_at: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO bank_detail_version (
            supplier_id, beneficiary_id, details_hash, bank_name, account_last4,
            source, verified_by, verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            supplier_id,
            beneficiary_id,
            details_hash,
            bank_name,
            account_last4,
            source,
            verified_by,
            verified_at,
        ),
    )
    return int(cursor.lastrowid)


def append_status_snapshot(
    conn: sqlite3.Connection,
    *,
    transfer_record_id: int,
    status: str,
    failure_code: str | None,
    failure_message: str | None,
    source: str,
    seen_at: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO status_snapshot (
            transfer_record_id, status, failure_code, failure_message, source, seen_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (transfer_record_id, status, failure_code, failure_message, source, seen_at),
    )
    return int(cursor.lastrowid)


def set_transfer_status(
    conn: sqlite3.Connection,
    transfer_record_id: int,
    *,
    status: str,
    dispatch_date: str | None,
) -> None:
    """Update status from a poll. Does not touch a stored failure reason."""
    conn.execute(
        """
        UPDATE transfer_record
        SET status = ?, dispatch_date = COALESCE(?, dispatch_date)
        WHERE id = ?
        """,
        (status, dispatch_date, transfer_record_id),
    )


def set_obligation_status(conn: sqlite3.Connection, obligation_id: int, status: str) -> None:
    conn.execute("UPDATE obligation SET status = ? WHERE id = ?", (status, obligation_id))


def open_duplicate_incident(
    conn: sqlite3.Connection,
    *,
    obligation_id: int,
    airwallex_id: str,
) -> int | None:
    """Open one incident for an extra live payment. Returns None if that payment is already flagged."""
    marker = f"duplicate:{airwallex_id}"
    existing = conn.execute(
        "SELECT id FROM incident WHERE obligation_id = ? AND claim_json = ?",
        (obligation_id, marker),
    ).fetchone()
    if existing:
        return None
    cursor = conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, ?, 'DUPLICATE')",
        (obligation_id, marker),
    )
    return int(cursor.lastrowid)


def insert_external_transfer(
    conn: sqlite3.Connection,
    *,
    airwallex_id: str,
    request_id: str | None,
    obligation_id: int,
    amount: str,
    currency: str,
    status: str,
    reference: str,
) -> int | None:
    """Tag an untagged look-alike. Returns None when that Airwallex id is already stored."""
    existing = conn.execute(
        "SELECT id FROM transfer_record WHERE airwallex_id = ?",
        (airwallex_id,),
    ).fetchone()
    if existing:
        return None
    cursor = conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, obligation_id, origin, amount, currency, status, reference
        ) VALUES (?, ?, ?, 'external', ?, ?, ?, ?)
        """,
        (airwallex_id, request_id, obligation_id, amount, currency, status, reference),
    )
    return int(cursor.lastrowid)


def remember_failure_reason(
    conn: sqlite3.Connection,
    transfer_record_id: int,
    failure_message: str | None,
) -> None:
    """Keep a stored reason. A null read, as from an unpinned GET, does not clear it."""
    if not failure_message:
        return
    code = failure_message.split(":", 1)[0].strip() if ":" in failure_message else None
    conn.execute(
        """
        UPDATE transfer_record
        SET failure_message = ?, failure_code = ?
        WHERE id = ?
        """,
        (failure_message, code, transfer_record_id),
    )


def attach_live_transfer(conn: sqlite3.Connection, obligation_id: int, transfer_record_id: int) -> None:
    """One live transfer per obligation. A CANCELLED transfer may be replaced."""
    obligation = conn.execute("SELECT live_transfer_id FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    current_id = obligation["live_transfer_id"]
    if current_id is not None and int(current_id) != transfer_record_id:
        current = conn.execute("SELECT status FROM transfer_record WHERE id = ?", (current_id,)).fetchone()
        if current is None or current["status"] != "CANCELLED":
            raise LedgerError("obligation already has a live transfer")
    conn.execute(
        "UPDATE obligation SET live_transfer_id = ? WHERE id = ?",
        (transfer_record_id, obligation_id),
    )


def append_audit(
    conn: sqlite3.Connection,
    *,
    incident_id: int | None,
    kind: str,
    detail: str,
    created_at: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO audit_event (incident_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (incident_id, kind, detail, created_at),
    )
    return int(cursor.lastrowid)


def lock_fx_quote(
    conn: sqlite3.Connection,
    *,
    quote_id: str,
    pair: str,
    rate: str,
    amount: str,
    valid_to: str,
) -> None:
    conn.execute(
        """
        INSERT INTO fx_quote (quote_id, pair, rate, amount, valid_to, state)
        VALUES (?, ?, ?, ?, ?, 'LOCKED')
        """,
        (quote_id, pair, rate, amount, valid_to),
    )


def consume_fx_quote(conn: sqlite3.Connection, quote_id: str, conversion_id: str) -> None:
    """Claim a locked quote. The update is the lock: a second caller changes no row.

    Pass an empty conversion id to claim before create_conversion, then
    record_fx_conversion once the conversion id exists.
    """
    cursor = conn.execute(
        """
        UPDATE fx_quote
        SET state = 'CONSUMED', consumed_by = ?
        WHERE quote_id = ? AND state = 'LOCKED'
        """,
        (conversion_id, quote_id),
    )
    if cursor.rowcount != 1:
        raise LedgerError("quote is not available")


def record_fx_conversion(conn: sqlite3.Connection, quote_id: str, conversion_id: str) -> None:
    """Store the conversion id on a quote claimed before the conversion was sent."""
    cursor = conn.execute(
        """
        UPDATE fx_quote SET consumed_by = ?
        WHERE quote_id = ? AND state = 'CONSUMED' AND consumed_by = ''
        """,
        (conversion_id, quote_id),
    )
    if cursor.rowcount != 1:
        raise LedgerError("quote is not available")


def release_unsent_fx_quote(conn: sqlite3.Connection, quote_id: str) -> None:
    """Return a claimed quote to LOCKED when the conversion was not created."""
    cursor = conn.execute(
        """
        UPDATE fx_quote SET state = 'LOCKED', consumed_by = NULL
        WHERE quote_id = ? AND state = 'CONSUMED' AND consumed_by = ''
        """,
        (quote_id,),
    )
    if cursor.rowcount != 1:
        raise LedgerError("quote is not available")


def expire_fx_quote(conn: sqlite3.Connection, quote_id: str) -> None:
    """A consumed quote stays consumed. A locked quote can be marked expired."""
    row = conn.execute("SELECT state FROM fx_quote WHERE quote_id = ?", (quote_id,)).fetchone()
    if row is None or row["state"] == "CONSUMED":
        raise LedgerError("quote is not available")
    if row["state"] == "EXPIRED":
        return
    conn.execute(
        "UPDATE fx_quote SET state = 'EXPIRED' WHERE quote_id = ? AND state = 'LOCKED'",
        (quote_id,),
    )

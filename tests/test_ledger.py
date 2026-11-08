"""Ledger seed and append-only rules. No network."""

import pytest

from backend.ledger import (
    HERO_AMOUNT,
    HERO_CURRENCY,
    HERO_INVOICE,
    HERO_SUPPLIER,
    MODEL_TABLES,
    LedgerError,
    append_bank_detail_version,
    append_status_snapshot,
    attach_live_transfer,
    connect,
    consume_fx_quote,
    hero_bundle,
    lock_fx_quote,
    remember_failure_reason,
    seed_hero,
    table_names,
)


def test_seed_creates_lumen_invoice_and_its_transfer():
    conn = connect()
    incident_id = seed_hero(conn)
    bundle = hero_bundle(conn, incident_id)

    assert bundle["supplier"]["name"] == HERO_SUPPLIER
    assert bundle["supplier"]["phone_on_file"] == "+49-30-000000"
    assert bundle["obligation"]["invoice_ref"] == HERO_INVOICE
    assert bundle["obligation"]["amount"] == HERO_AMOUNT
    assert bundle["obligation"]["currency"] == HERO_CURRENCY
    assert bundle["obligation"]["status"] == "OPEN"
    assert bundle["transfers"][0]["reference"] == HERO_INVOICE
    assert bundle["transfers"][0]["origin"] == "settlewell"
    assert bundle["transfers"][0]["status"] == "SENT"
    assert bundle["transfers"][0]["request_id"] == "hero-inv2291"
    assert bundle["obligation"]["live_transfer_id"] == bundle["transfers"][0]["id"]
    assert bundle["versions"][0]["bank_name"] == "Commerzbank"
    assert bundle["versions"][0]["source"] == "onboarding"
    assert MODEL_TABLES <= table_names(conn)
    assert seed_hero(conn) == incident_id


def test_bank_detail_versions_and_snapshots_are_append_only():
    conn = connect()
    incident_id = seed_hero(conn)
    bundle = hero_bundle(conn, incident_id)
    supplier_id = bundle["supplier"]["id"]
    transfer_id = bundle["transfers"][0]["id"]

    append_bank_detail_version(
        conn,
        supplier_id=supplier_id,
        beneficiary_id="ben_new",
        details_hash="callback-v2",
        bank_name="Deutsche Kreditbank",
        account_last4="7781",
        source="verified_callback",
        verified_by="finance",
        verified_at="2026-11-03T12:00:00+00:00",
    )
    append_status_snapshot(
        conn,
        transfer_record_id=transfer_id,
        status="SENT",
        failure_code=None,
        failure_message=None,
        source="poll",
        seen_at="2026-11-03T12:00:00+00:00",
    )
    conn.commit()

    versions = conn.execute(
        "SELECT bank_name, source FROM bank_detail_version ORDER BY id"
    ).fetchall()
    assert [row["bank_name"] for row in versions] == ["Commerzbank", "Deutsche Kreditbank"]
    assert versions[0]["source"] == "onboarding"
    assert conn.execute("SELECT COUNT(*) AS n FROM status_snapshot").fetchone()["n"] == 1


def test_null_failure_read_does_not_wipe_a_stored_reason():
    conn = connect()
    incident_id = seed_hero(conn)
    transfer_id = hero_bundle(conn, incident_id)["transfers"][0]["id"]
    remember_failure_reason(conn, transfer_id, "90701: Account closed")
    remember_failure_reason(conn, transfer_id, None)
    conn.commit()

    row = conn.execute(
        "SELECT failure_code, failure_message FROM transfer_record WHERE id = ?",
        (transfer_id,),
    ).fetchone()
    assert row["failure_code"] == "90701"
    assert row["failure_message"] == "90701: Account closed"


def test_a_second_live_transfer_is_rejected_until_the_first_is_cancelled():
    conn = connect()
    incident_id = seed_hero(conn)
    bundle = hero_bundle(conn, incident_id)
    obligation_id = bundle["obligation"]["id"]
    extra = conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, obligation_id, origin, amount, currency, status, reference
        ) VALUES ('awx-extra', 'colleague-1', ?, 'external', '4200.00', 'EUR', 'SCHEDULED', 'INV-2291')
        """,
        (obligation_id,),
    )
    with pytest.raises(LedgerError):
        attach_live_transfer(conn, obligation_id, int(extra.lastrowid))

    conn.execute(
        "UPDATE transfer_record SET status = 'CANCELLED' WHERE id = ?",
        (bundle["transfers"][0]["id"],),
    )
    attach_live_transfer(conn, obligation_id, int(extra.lastrowid))
    conn.commit()
    live = conn.execute("SELECT live_transfer_id FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    assert live["live_transfer_id"] == extra.lastrowid


def test_an_fx_quote_can_be_consumed_only_once():
    conn = connect()
    seed_hero(conn)
    lock_fx_quote(
        conn,
        quote_id="quo_1",
        pair="USD/EUR",
        rate="1.13",
        amount="100.00",
        valid_to="2026-11-03T12:01:00+00:00",
    )
    consume_fx_quote(conn, "quo_1", "con_1")
    with pytest.raises(LedgerError):
        consume_fx_quote(conn, "quo_1", "con_2")


def test_old_db_with_narrow_intent_check_migrates_without_losing_rows(tmp_path):
    from backend.execute import open_intent
    from backend.ledger import SCHEMA, migrate

    path = tmp_path / "old.db"
    old = connect(path)
    incident_id = seed_hero(old)
    obligation_id = old.execute("SELECT obligation_id FROM incident WHERE id = ?", (incident_id,)).fetchone()[0]
    ref = open_intent(
        old, kind="replacement", incident_id=incident_id, obligation_id=obligation_id,
        action="REPLACE", amount="4200.00", currency="EUR", beneficiary_id="b-1",
        details_hash="h" * 8, proposer="alice", quote_id="q-1", created_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-02T00:00:00+00:00",
    )
    old.execute(
        "INSERT INTO payout_funding (request_id, intent_id, incident_id, obligation_id, currency, amount, "
        "fee_budget, fee_currency, fee_source, reserve_floor, available_before, state, hold_state, "
        "after_state, created_at) VALUES ('r-1', ?, ?, ?, 'EUR', '4200.00', '13.47', 'EUR', 'test', '0', "
        "'9000', 'ACCEPTED', 'HELD', 'PENDING', '2026-01-01')",
        (ref.id, incident_id, obligation_id),
    )
    old.commit()
    # Downgrade only execution_intent to the pre-outreach CHECK, as an old persisted DB has it.
    old_sql = SCHEMA.split("CREATE TABLE IF NOT EXISTS execution_intent", 1)[1].split(");", 1)[0]
    old_sql = old_sql.replace(", 'outreach'", "")
    old.execute("CREATE TABLE execution_intent_old" + old_sql + ")")
    old.execute("INSERT INTO execution_intent_old SELECT * FROM execution_intent")
    old.execute("DROP TABLE execution_intent")
    old.execute("ALTER TABLE execution_intent_old RENAME TO execution_intent")
    old.commit()
    with pytest.raises(Exception, match="CHECK"):
        old.execute(
            "INSERT INTO execution_intent (kind, action, amount, currency, beneficiary_id, details_hash, "
            "state, created_at) VALUES ('outreach', 'x', '0', 'EUR', 's', 'h', 'proposed', 'now')"
        )
    old.rollback()
    before_intents = [tuple(r) for r in old.execute("SELECT * FROM execution_intent ORDER BY id")]
    before_approvals = [tuple(r) for r in old.execute("SELECT * FROM approval ORDER BY id")]
    before_funding = [tuple(r) for r in old.execute("SELECT * FROM payout_funding")]
    old.close()

    conn = connect(path)
    migrate(conn)
    migrate(conn)  # idempotent
    assert [tuple(r) for r in conn.execute("SELECT * FROM execution_intent ORDER BY id")] == before_intents
    assert [tuple(r) for r in conn.execute("SELECT * FROM approval ORDER BY id")] == before_approvals
    assert [tuple(r) for r in conn.execute("SELECT * FROM payout_funding")] == before_funding
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'execution_intent_new'").fetchall()
    fresh = open_intent(
        conn, kind="outreach", incident_id=incident_id, obligation_id=obligation_id,
        action="REPLY_WAIT", amount="0", currency="EUR", beneficiary_id="supplier:1",
        details_hash="d" * 8, proposer="alice", created_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-02T00:00:00+00:00",
    )
    assert fresh.id > ref.id
    old_row = conn.execute("SELECT state, approval_id FROM execution_intent WHERE id = ?", (ref.id,)).fetchone()
    assert old_row["state"] == "proposed" and old_row["approval_id"] is not None

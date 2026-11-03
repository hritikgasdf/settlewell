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
    assert bundle["transfers"][0]["request_id"] == "stl-inv2291-1"
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

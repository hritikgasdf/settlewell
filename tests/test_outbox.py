"""A supplier reply waits in the outbox until it is approved. Nothing is sent."""

from backend.approvals import FINANCE_APPROVER, ApprovalTerms, approve
from backend.console import render
from backend.ledger import connect
from backend.outbox import outbox_rows

_TERMS = ApprovalTerms(
    action="SEND_REPLY",
    amount="4200.00",
    currency="EUR",
    beneficiary_id="ben_onboarding",
    details_hash="onboarding-v1",
)


def test_a_reply_waits_until_approved_and_is_never_sent(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    conn = connect()
    waiting = render("/outbox", conn)
    assert 'class="state">waiting' in waiting
    assert "The payment is still on its way." in waiting
    assert 'class="state">approved' not in waiting
    assert "sent" not in waiting.lower()

    approval_id = conn.execute(
        "SELECT approval_id FROM outbox_message"
    ).fetchone()["approval_id"]
    approve(
        conn,
        approval_id,
        _TERMS,
        approver=FINANCE_APPROVER,
        used_at="2026-11-03T12:30:00+00:00",
    )
    approved = render("/outbox", conn)
    assert 'class="state">approved' in approved
    assert 'class="state">waiting' not in approved
    assert [row["state"] for row in outbox_rows(conn)] == ["approved"]

    conn.execute("UPDATE outbox_message SET body = ?", ("Hello <b>there</b>",))
    conn.commit()
    escaped = render("/outbox", conn)
    assert "Hello &lt;b&gt;there&lt;/b&gt;" in escaped
    assert "<b>" not in escaped

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(outbox_message)")}
    assert "sent_at" not in columns

"""The first four hero beats are clickable in mock mode. No network."""

import re

import pytest

from backend.console import ConsoleError, render
from backend.ledger import connect
from settlewell.flags import mock_mode


def _hrefs(page: str) -> list[str]:
    return re.findall(r'href="(/incidents/\d+)"', page)


def test_the_console_refuses_to_run_outside_mock(monkeypatch):
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    assert mock_mode() is False
    with pytest.raises(ConsoleError) as caught:
        render("/")
    assert caught.value.code == "mock"


def test_inbox_links_open_timeline_decision_approval_and_audit(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    conn = connect()
    inbox = render("/", conn)
    hrefs = _hrefs(inbox)
    assert hrefs == ["/incidents/1", "/incidents/2", "/incidents/3", "/incidents/4"]
    assert "Late SENT · INV-2291 · WAIT" in inbox
    assert "Scheduled duplicate · INV-2291 · STOP_DUPLICATE" in inbox
    assert "Still processing · INV-2302 · WAIT" in inbox
    assert "Already paid · INV-2404 · WAIT" in inbox

    pages = [render(href, conn) for href in hrefs]
    for page in pages:
        assert 'id="timeline"' in page
        assert 'id="decision"' in page
        assert 'id="approval"' in page
        assert 'id="audit"' in page
        assert 'href="/"' in page
        assert "ap-clerk" in page
        assert "not used" in page

    late, duplicate, processing, paid = pages
    assert "R-NO-REPLACE-IN-FLIGHT" in late
    assert "dispatch_date 2026-11-04 is after transfer_date 2026-11-03" in late
    assert "R-STOP-DUPLICATE-SCHEDULED" in duplicate
    assert "untagged SCHEDULED transfer awx-dup" in duplicate
    assert "R-NO-REPLACE-IN-FLIGHT" in processing
    assert "status PROCESSING, still in flight" in processing
    assert "R-WAIT-PAID" in paid
    assert "status PAID, no replace" in paid
    joined = inbox + "".join(pages)
    assert "AIRWALLEX" not in joined
    assert "whsec" not in joined
    assert 'href="/ledger"' in inbox

    ledger = render("/ledger", conn)
    assert "paid once" in ledger
    assert "Incident cost €13.47" in ledger
    assert "€800.00" in ledger
    assert "€5200.00" in ledger
    assert "USD/EUR" in ledger
    assert 'id="eval-strip"' in ledger
    assert "paid once" in ledger
    conn.execute(
        "UPDATE balance_snapshot SET available = ? WHERE currency = 'EUR' AND label = 'before'",
        ("1<script>",),
    )
    conn.commit()
    escaped = render("/ledger", conn)
    assert "1&lt;script&gt;" in escaped
    assert "<script>" not in escaped

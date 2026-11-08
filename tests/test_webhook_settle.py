"""Settle window, ordering, durable dedupe, worker and HTTP receiver. Offline; fakes bound to real terms."""

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from test_webhooks import (
    NOW,
    SECRET,
    _deliver,
    _event,
    _hero,
    _obligation,
    _reason,
    _signed,
    _status,
)

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import BalanceHistoryPage, Conversion, Transfer, TransferPage
from backend.ledger import connect
from backend.reconcile import poll_incident
from backend.webhooks import WebhookError, incident_pending, receive_webhook, settle
from backend.worker import Worker, tick


class Remote:
    """Fake with the hero's real terms. It has no write methods, so any POST would raise."""

    def __init__(self, status="PAID", fail=False, reason=None):
        self.status, self.fail, self.reason, self.reads = status, fail, reason, 0

    def get_transfer(self, transfer_id):
        self.reads += 1
        if self.fail:
            raise AirwallexError(503, None, "down")
        return Transfer(
            id=transfer_id, status=self.status, request_id="stl-inv2291-1",
            transfer_amount=Decimal("4200.00"), transfer_currency="EUR", reference="INV-2291",
            failure_reason=self.reason,
        )

    def balance_history(self, **_kwargs):
        return BalanceHistoryPage(items=(), has_more=False, page_after=None)

    def list_transfers(self, **_kwargs):
        return TransferPage(items=(self.get_transfer("awx-hero"),), page_after=None)

    def find_conversion(self, request_id):
        return getattr(self, "conversion", None)


def _pending(conn):
    return conn.execute("SELECT * FROM webhook_state").fetchall()


def _paid(conn):
    _deliver(conn, _event("evt-paid", "payout.transfer.paid", NOW, "PAID"), NOW)


def _kinds(conn, incident_id):
    return [r["kind"] for r in conn.execute("SELECT kind FROM audit_event WHERE incident_id = ?", (incident_id,))]


def test_paid_payload_never_unlocks_and_poll_waits_for_the_window():
    conn = connect()
    incident_id = _hero(conn, status="SENT")
    _paid(conn)

    assert _status(conn) == "SENT"
    assert _obligation(conn) == "OPEN"
    remote = Remote("PAID")
    assert settle(conn, remote, NOW + 9_999) == []
    assert remote.reads == 0 and _obligation(conn) == "OPEN"
    poll_incident(conn, remote, incident_id, seen_at="2026-11-03T12:00:00+00:00")
    assert remote.reads == 0 and incident_pending(conn, incident_id)
    assert settle(conn, remote, NOW + 10_000) == ["awx-hero"]
    assert remote.reads > 0 and _status(conn) == "PAID"
    assert not _pending(conn)


def test_authoritative_poll_failure_holds_and_audits():
    conn = connect()
    incident_id = _hero(conn, status="SENT")
    _paid(conn)

    assert settle(conn, Remote(fail=True), NOW + 11_000) == []
    assert len(_pending(conn)) == 1 and _obligation(conn) == "OPEN"
    assert "webhook_poll_failed" in _kinds(conn, incident_id)


def test_failed_inside_the_window_wins_over_paid():
    conn = connect()
    _hero(conn, status="SENT")
    _paid(conn)
    _deliver(conn, _event("evt-failed", "payout.transfer.failed", NOW + 1_000, "FAILED",
                          failure_reason="90701: Account closed"), NOW + 1_000, now=NOW + 1_000)

    assert _status(conn) == "FAILED" and _reason(conn) == "90701: Account closed"
    assert _pending(conn)[0]["settle_until_ms"] == NOW + 11_000
    settle(conn, Remote("FAILED", reason="90701: Account closed"), NOW + 20_000)
    assert _obligation(conn) == "OPEN" and _status(conn) == "FAILED"


def test_older_paid_does_not_extend_the_window_or_erase_failure():
    conn = connect()
    _hero(conn, status="SENT")
    _deliver(conn, _event("evt-failed", "payout.transfer.failed", NOW + 1_000, "FAILED"), NOW + 1_000, now=NOW + 1_000)
    before = dict(_pending(conn)[0])
    _deliver(conn, _event("evt-paid", "payout.transfer.paid", NOW, "PAID"), NOW, now=NOW + 5_000)

    assert dict(_pending(conn)[0]) == before and _status(conn) == "FAILED"


def test_same_timestamp_conflict_is_conservative():
    conn = connect()
    _hero(conn, status="SENT")
    _paid(conn)
    _deliver(conn, _event("evt-failed", "payout.transfer.failed", NOW, "FAILED"), NOW)
    assert _status(conn) == "FAILED" and _pending(conn)[0]["latest_status"] == "FAILED"


def test_window_survives_restart_and_dedupe_is_durable(tmp_path):
    path = str(tmp_path / "w.db")
    conn = connect(path)
    incident_id = _hero(conn, status="SENT")
    _paid(conn)
    conn.close()
    conn = connect(path)
    assert incident_pending(conn, incident_id)
    resigned = _deliver(conn, _event("evt-paid", "payout.transfer.paid", NOW, "PAID"), NOW + 2_000, now=NOW + 2_000)
    assert resigned.outcome == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM webhook_event").fetchone()[0] == 1


def test_late_failure_after_closure_reopens_via_shared_reconciler():
    conn = connect()
    incident_id = _hero(conn, status="SENT")
    _paid(conn)
    settle(conn, Remote("PAID"), NOW + 10_000)
    closed = _obligation(conn)
    _deliver(conn, _event("evt-ret", "payout.transfer.failed", NOW + 60_000, "FAILED"), NOW + 60_000, now=NOW + 60_000)
    assert _obligation(conn) != "PAID_ONCE"
    settle(conn, Remote("FAILED", reason="90802: returned"), NOW + 70_000)
    assert _obligation(conn) != "PAID_ONCE"
    if closed == "PAID_ONCE":
        assert "incident_reopened" in _kinds(conn, incident_id)


@pytest.mark.parametrize("kwargs, code", [
    ({"secret": "   "}, "unconfigured"),
    ({"x_signature": "00"}, "signature"),
    ({"x_timestamp": "abc"}, "signature"),
])
def test_receiver_rejects_before_parsing(kwargs, code):
    conn = connect()
    _hero(conn)
    raw, sig = _signed(_event("e1", "payout.transfer.paid", NOW, "PAID"), NOW)
    args = {"secret": SECRET, "raw_body": raw, "x_timestamp": str(NOW), "x_signature": sig, "now_ms": NOW}
    args.update(kwargs)
    with pytest.raises(WebhookError) as caught:
        receive_webhook(conn, **args)
    assert caught.value.code == code
    assert conn.execute("SELECT COUNT(*) FROM webhook_event").fetchone()[0] == 0


def test_future_timestamp_and_oversize_body_are_rejected():
    conn = connect()
    _hero(conn)
    with pytest.raises(WebhookError) as caught:
        _deliver(conn, _event("e2", "payout.transfer.paid", NOW, "PAID"), NOW + 120_000)
    assert caught.value.code == "stale"
    with pytest.raises(WebhookError) as caught:
        receive_webhook(conn, secret=SECRET, raw_body=b"x" * 70_000, x_timestamp=str(NOW),
                        x_signature="00", now_ms=NOW)
    assert caught.value.code == "body"


def test_worker_tick_recovers_a_missed_webhook_by_polling(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    _hero(conn, status="SENT")
    remote = Remote("PAID")
    report = tick(conn, remote, NOW)
    assert report["polled"] and _status(conn) == "PAID"


def test_worker_tick_skips_polling_inside_a_pending_window_then_settles():
    conn = connect()
    incident_id = _hero(conn, status="SENT")
    _paid(conn)
    remote = Remote("PAID")
    report = tick(conn, remote, NOW + 1_000)
    assert incident_id not in report["polled"] and remote.reads == 0
    report = tick(conn, remote, NOW + 10_000)
    assert report["settled"] == ["awx-hero"]


def test_worker_resumes_a_consumed_approval_crash_via_execute(monkeypatch):
    conn = connect()
    incident_id = _hero(conn, status="SENT")
    conn.execute("INSERT INTO approval (incident_id, action, binding_hash, used_at, approver, proposer) "
                 "VALUES (?, 'replacement', 'h', '2026-11-03T12:00:00+00:00', 'a', 'b')", (incident_id,))
    approval = conn.execute("SELECT id FROM approval").fetchone()["id"]
    conn.execute("INSERT INTO execution_intent (incident_id, obligation_id, kind, action, amount, currency, "
                 "beneficiary_id, details_hash, state, approval_id, created_at) VALUES "
                 "(?, 1, 'replacement', 'replacement', '4200.00', 'EUR', 'b', 'd', 'proposed', ?, 'x')",
                 (incident_id, approval))
    conn.commit()
    calls = []
    monkeypatch.setattr("backend.worker.execute", lambda c, cl, intent_id, as_of: calls.append(intent_id))
    report = tick(conn, Remote("SENT"), NOW)
    assert calls == report["resumed"] and len(calls) == 1  # same intent, no new approval or intent


def test_worker_holds_when_resume_is_refused():
    conn = connect()
    incident_id = _hero(conn, status="SENT")
    conn.execute("INSERT INTO approval (incident_id, action, binding_hash, used_at, approver, proposer) "
                 "VALUES (?, 'cancel', 'h', '2026-11-03T12:00:00+00:00', 'a', 'b')", (incident_id,))
    conn.execute("INSERT INTO execution_intent (incident_id, obligation_id, kind, action, amount, currency, "
                 "beneficiary_id, details_hash, state, approval_id, created_at) VALUES "
                 "(?, 1, 'cancel', 'cancel', '1', 'EUR', 'b', 'd', 'proposed', 1, 'x')", (incident_id,))
    conn.commit()
    report = tick(conn, Remote("SENT"), NOW)
    assert report["held"] and not report["resumed"]
    assert conn.execute("SELECT state FROM execution_intent").fetchone()["state"] == "proposed"
    assert "worker_resume_held" in _kinds(conn, incident_id)


class _NoPost(Remote):
    """Remote that records any write attempt instead of performing one."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.posts = []

    def create_conversion(self, **kwargs):
        self.posts.append(kwargs)
        raise AssertionError("unexpected conversion POST")


def test_worker_refuses_to_bind_a_conversion_without_an_owning_approved_intent():
    conn = connect()
    _hero(conn)
    conn.execute("INSERT INTO fx_quote (quote_id, pair, rate, amount, valid_to, state, consumed_by) "
                 "VALUES ('q-1', 'USDEUR', '0.9', '100', '2099-01-01T00:00:00+00:00', 'CONSUMED', '')")
    conn.commit()
    remote = _NoPost("SENT")
    remote.conversion = Conversion(conversion_id="conv-1", status="SETTLED", quote_id="q-1")
    report = tick(conn, remote, NOW)  # a perfectly matching remote conversion, but nobody owns it
    assert report["conversions"] == [] and report["resumed"] == []
    assert conn.execute("SELECT state, consumed_by FROM fx_quote").fetchone()[:] == ("CONSUMED", "")
    assert conn.execute("SELECT COUNT(*) FROM execution_intent").fetchone()[0] == 0
    assert remote.posts == []


def _fx_recovery_setup(path=None):
    from test_fx_quote import EARLY, _proposed

    from backend.execute import execute

    conn = connect(path) if path else connect()
    _proposed(conn)
    intent = conn.execute("SELECT id FROM execution_intent WHERE quote_id = 'quo_1'").fetchone()["id"]
    desk = Remote("SENT")  # real read fakes; its conversion POST crashes after the remote accepted it
    desk.converted = []
    found = {}

    def create_conversion(**kwargs):
        desk.converted.append(kwargs)
        found[kwargs["request_id"]] = Conversion(conversion_id="con_crash", status="SETTLED",
                                                 quote_id=kwargs["quote_id"])
        raise RuntimeError("crash")

    desk.create_conversion = create_conversion
    desk.find_conversion = found.get
    with pytest.raises(RuntimeError, match="crash"):
        execute(conn, desk, intent, as_of=EARLY)
    return conn, desk, intent


def test_worker_recovers_a_conversion_through_its_owning_intent_with_zero_posts(tmp_path):
    path = str(tmp_path / "fx.db")
    conn, desk, intent = _fx_recovery_setup(path)
    request_id = conn.execute("SELECT request_id FROM execution_intent WHERE id = ?", (intent,)).fetchone()[0]
    assert request_id and desk.converted[0]["request_id"] == request_id
    conn.close()
    conn = connect(path)  # restart: only durable state survives
    report = tick(conn, desk, NOW)
    assert intent in report["conversions"]
    assert len(desk.converted) == 1  # recovery by request id, never a second POST
    row = conn.execute("SELECT remote_id, request_id FROM execution_intent WHERE id = ?", (intent,)).fetchone()
    assert (row["remote_id"], row["request_id"]) == ("con_crash", request_id)
    assert conn.execute("SELECT state, consumed_by FROM fx_quote WHERE quote_id = 'quo_1'").fetchone()[:] == (
        "CONSUMED", "con_crash")


@pytest.mark.parametrize("found_quote", ["other", None])
def test_worker_does_not_bind_a_foreign_or_unbound_conversion(found_quote):
    conn, desk, intent = _fx_recovery_setup()
    desk.find_conversion = lambda _rid: Conversion(conversion_id="con_x", status="SETTLED", quote_id=found_quote)
    report = tick(conn, desk, NOW)
    assert report["conversions"] == [] and intent in report["held"]
    assert conn.execute("SELECT remote_id FROM execution_intent WHERE id = ?", (intent,)).fetchone()[0] is None
    assert conn.execute("SELECT consumed_by FROM fx_quote WHERE quote_id = 'quo_1'").fetchone()[0] == ""
    assert len(desk.converted) == 1


def test_fx_execute_rejects_missing_quote_wrong_owner_and_unapproved():
    from test_fx_quote import EARLY, _proposed

    from backend.approvals import ApprovalTerms
    from backend.fx_quote import FxQuoteError, execute_fx_quote

    conn = connect()
    terms, _ = _proposed(conn)
    row = conn.execute("SELECT id, approval_id FROM execution_intent WHERE quote_id = 'quo_1'").fetchone()
    desk = _NoPost("SENT")

    def attempt(approval_id, t, **kw):
        with pytest.raises(FxQuoteError):
            execute_fx_quote(conn, desk, approval_id, t, as_of=EARLY, **kw)

    attempt(row["approval_id"], ApprovalTerms(**{**terms.__dict__, "quote_id": None}), intent_id=row["id"])
    attempt(row["approval_id"], ApprovalTerms(**{**terms.__dict__, "quote_id": "nope"}), intent_id=row["id"])
    attempt(row["approval_id"] + 99, terms, intent_id=row["id"])  # wrong approval
    assert desk.posts == [] and conn.execute("SELECT state FROM fx_quote").fetchone()[0] == "LOCKED"
    assert conn.execute("SELECT state, remote_id FROM execution_intent").fetchone()[:] == ("proposed", None)


def test_worker_thread_starts_and_stops_cleanly(tmp_path):
    path = str(tmp_path / "w.db")
    _hero(connect(path))
    worker = Worker(lambda: connect(path), lambda _c: Remote("SENT"), interval=0.05, clock_ms=lambda: NOW)
    worker.start()
    worker.stop()
    assert worker._thread is None


def _http(tmp_path, monkeypatch, secret=SECRET):
    from fastapi.testclient import TestClient

    from backend.api import create_app

    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    if secret is None:
        monkeypatch.delenv("AIRWALLEX_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("AIRWALLEX_WEBHOOK_SECRET", secret)
    path = str(tmp_path / "api.db")
    app = create_app(path, now_ms=lambda: NOW)
    return TestClient(app, base_url="http://127.0.0.1:8000"), path


def _post(client, body, ts=NOW, bad=False):
    raw = json.dumps(body, separators=(",", ":")).encode()
    sig = hmac.new(SECRET.encode(), str(ts).encode() + raw, hashlib.sha256).hexdigest()
    headers = {"x-timestamp": str(ts), "x-signature": "0" * 64 if bad else sig}
    return client.post("/api/webhooks/airwallex", content=raw, headers=headers)


def test_http_endpoint_signature_dedupe_and_failclosed(tmp_path, monkeypatch):
    client, path = _http(tmp_path, monkeypatch)
    conn = connect(path)
    conn.execute("UPDATE transfer_record SET airwallex_id = 'awx-hero' WHERE id = (SELECT MIN(id) FROM transfer_record)")
    conn.commit()
    event = _event("evt-http", "payout.transfer.failed", NOW, "FAILED")
    assert _post(client, event, bad=True).status_code == 401
    assert _post(client, event, ts=NOW - 600_000).status_code == 400
    assert _post(client, event).status_code == 200
    assert _post(client, event).json()["outcome"] == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM webhook_event").fetchone()[0] == 1
    assert client.post("/api/webhooks/airwallex", content=b"{}").status_code == 401


def test_http_endpoint_without_secret_is_503(tmp_path, monkeypatch):
    client, _ = _http(tmp_path, monkeypatch, secret=" ")
    assert _post(client, _event("e", "payout.transfer.paid", NOW, "PAID")).status_code == 503


class _CancelApi:
    """DuplicateApi bound to the real duplicate terms, plus the read-only calls a poll needs."""

    def __new__(cls, status):
        from test_stop_duplicate import DuplicateApi

        class Api(DuplicateApi):
            def balance_history(self, **_kwargs):
                return BalanceHistoryPage(items=(), has_more=False, page_after=None)

            def list_transfers(self, **_kwargs):
                return TransferPage(items=(), page_after=None)

        return Api(status)


def _intent_state(conn):
    return conn.execute("SELECT state, remote_status FROM execution_intent").fetchone()


def test_worker_resumes_a_consumed_approval_cancel_once_through_execute():
    from test_stop_duplicate import _ready

    conn = connect()
    _ready(conn)  # real proposed cancel intent whose single-use approval is consumed, then "crash"
    api = _CancelApi("SCHEDULED")
    report = tick(conn, api, NOW)
    assert len(report["resumed"]) == 1 and api.cancels == 1
    assert _intent_state(conn)["state"] == "executed"
    tick(conn, api, NOW + 5_000)
    assert api.cancels == 1  # same intent is never resent
    assert conn.execute("SELECT COUNT(*) FROM approval WHERE used_at IS NOT NULL").fetchone()[0] == 1


def test_worker_unknown_cancel_status_rereads_and_never_posts():
    from test_stop_duplicate import _ready

    conn = connect()
    _ready(conn)
    api = _CancelApi("CANCELLED")  # the earlier cancel landed but the local write was lost
    report = tick(conn, api, NOW)
    assert api.cancels == 0  # read-only refresh of the same owned target
    # we never claimed this cancel, so a CANCELLED remote is not proof it was ours: stay held
    assert _intent_state(conn)["state"] == "proposed" and report["held"] and not report["resumed"]


def test_worker_finalizes_a_claimed_cancel_that_landed_without_resending():
    from test_stop_duplicate import _ready

    conn = connect()
    _ready(conn)
    conn.execute("UPDATE execution_intent SET state = 'executed', request_id = 'awx-dup'")  # claim, then crash
    conn.commit()
    api = _CancelApi("CANCELLED")
    report = tick(conn, api, NOW)
    assert api.cancels == 0 and len(report["resumed"]) == 1
    intent = conn.execute("SELECT remote_id, remote_status FROM execution_intent").fetchone()
    assert (intent["remote_id"], intent["remote_status"]) == ("awx-dup", "CANCELLED")
    assert conn.execute("SELECT status FROM transfer_record WHERE airwallex_id = 'awx-dup'").fetchone()[0] == "CANCELLED"


def _sibling(conn, transfer_id="awx-sib"):
    conn.execute(
        "INSERT INTO transfer_record (airwallex_id, obligation_id, origin, amount, currency, status, reference) "
        "SELECT ?, obligation_id, 'external', amount, currency, 'SENT', reference FROM transfer_record "
        "WHERE airwallex_id = 'awx-hero'", (transfer_id,))
    conn.commit()


def _windows_left(conn):
    return sorted(r[0] for r in conn.execute("SELECT transfer_airwallex_id FROM webhook_state"))


def test_staggered_windows_on_one_obligation_hold_everything_until_all_mature(tmp_path):
    path = str(tmp_path / "s.db")
    conn = connect(path)
    incident_id = _hero(conn, status="SENT")
    _sibling(conn)
    _paid(conn)  # hero window ends NOW + 10_000
    _deliver(conn, _event("evt-sib", "payout.transfer.paid", NOW + 5_000, "PAID", id="awx-sib"), NOW + 5_000,
             now=NOW + 5_000)  # sibling window ends NOW + 15_000
    conn.close()
    conn = connect(path)  # reopen: windows are durable
    remote = Remote("PAID")
    assert settle(conn, remote, NOW + 10_000) == []  # hero matured, sibling has not
    assert remote.reads == 0 and _windows_left(conn) == ["awx-hero", "awx-sib"]
    assert _obligation(conn) == "OPEN" and incident_pending(conn, incident_id)
    report = tick(conn, remote, NOW + 12_000)
    assert report["settled"] == [] and incident_id not in report["polled"] and remote.reads == 0
    assert _windows_left(conn) == ["awx-hero", "awx-sib"] and _obligation(conn) == "OPEN"
    assert sorted(settle(conn, remote, NOW + 15_000)) == ["awx-hero", "awx-sib"]
    assert remote.reads > 0 and _windows_left(conn) == []


def test_a_window_on_a_sibling_transfer_blocks_the_obligation_poll_and_hold():
    conn = connect()
    incident_id = _hero(conn, status="SENT")
    _sibling(conn)
    _deliver(conn, _event("evt-sib", "payout.transfer.paid", NOW, "PAID", id="awx-sib"), NOW)
    assert incident_pending(conn, incident_id)  # the hero itself has no window
    remote = Remote("PAID")
    assert incident_id not in tick(conn, remote, NOW + 1_000)["polled"] and remote.reads == 0


def test_an_event_landing_during_the_authoritative_poll_is_not_overwritten(tmp_path):
    path = str(tmp_path / "race.db")
    conn = connect(path)
    incident_id = _hero(conn, status="SENT")
    _paid(conn)
    other = connect(path)  # a second connection: the receiver racing the worker

    class Racing(Remote):
        fired = False

        def get_transfer(self, transfer_id):
            if not self.fired:
                self.fired = True  # deterministic barrier: lands mid-read, before the decision
                _deliver(other, _event("evt-ret", "payout.transfer.failed", NOW + 9_000, "FAILED"),
                         NOW + 9_000, now=NOW + 10_000)
            return super().get_transfer(transfer_id)

    remote = Racing("PAID")
    assert settle(conn, remote, NOW + 10_000) == []  # stale PAID read must not authorise anything
    assert remote.fired
    row = conn.execute("SELECT event_id, latest_status, settle_until_ms FROM webhook_state").fetchone()
    assert tuple(row) == ("evt-ret", "FAILED", NOW + 20_000)  # newer generation kept, window not cleared
    assert _obligation(conn) != "PAID_ONCE" and _status(conn) != "PAID"
    assert incident_pending(conn, incident_id)
    assert settle(conn, Remote("FAILED", reason="90802: returned"), NOW + 19_999) == []
    assert settle(conn, Remote("FAILED", reason="90802: returned"), NOW + 20_000) == ["awx-hero"]
    assert _windows_left(conn) == [] and _obligation(conn) != "PAID_ONCE"


def _funded_setup():
    from test_funding_execute import NEED, _setup, rows

    return _setup(balances=[rows(str(NEED)), rows("777.77")])


def _open_window(conn, transfer_id):
    conn.execute(
        "INSERT INTO webhook_state (transfer_airwallex_id, latest_event_ts_ms, latest_status, settle_until_ms, "
        "event_id) VALUES (?, ?, 'PAID', ?, 'evt-w')", (transfer_id, NOW, NOW + 10_000))
    conn.commit()


def _durable(conn):
    return {
        "approval": [tuple(r) for r in conn.execute("SELECT id, used_at, binding_hash FROM approval ORDER BY id")],
        "intent": [tuple(r) for r in conn.execute("SELECT id, state, request_id, remote_id FROM execution_intent")],
        "funding": conn.execute("SELECT COUNT(*) FROM payout_funding").fetchone()[0],
        "snapshots": conn.execute("SELECT COUNT(*) FROM balance_snapshot").fetchone()[0],
        "transfers": conn.execute("SELECT COUNT(*) FROM transfer_record").fetchone()[0],
    }


@pytest.mark.parametrize("window_on", ["original", "sibling"])
def test_a_valid_funded_payout_refuses_during_a_settle_window_then_sends_exactly_once(window_on):
    from backend.execute import ExecuteError, execute

    conn, client, ref = _funded_setup()
    if window_on == "sibling":
        _sibling(conn)
    _open_window(conn, "awx-hero" if window_on == "original" else "awx-sib")
    before = _durable(conn)
    assert before["approval"][0][1] and before["intent"][0][1:] == ("proposed", None, None)  # genuinely approved

    with pytest.raises(ExecuteError) as caught:  # standalone
        execute(conn, client, ref.id, as_of="2026-11-03T12:00:00+00:00")
    assert caught.value.code == "pending_webhook"
    report = tick(conn, client, NOW + 1_000)  # worker resume of the same consumed-approval intent
    assert ref.id in report["held"] and not report["resumed"]
    assert client.created == [] and client.conversions == []  # zero POST, no funding conversion
    assert _durable(conn) == before  # claim, grant, funding: nothing written, no regrant

    conn.execute("DELETE FROM webhook_state")  # window gone: the very same grant now pays exactly once
    conn.execute("DELETE FROM transfer_record WHERE airwallex_id = 'awx-sib'")  # sibling was only a window carrier
    conn.commit()
    result = execute(conn, client, ref.id, as_of="2026-11-03T12:00:00+00:00")
    assert result.remote_id == "awx-new" and len(client.created) == 1 and client.conversions == []
    after = _durable(conn)
    assert after["approval"] == before["approval"] and after["funding"] == 1


def test_positive_control_the_same_funded_payout_without_a_window_sends_once():
    from backend.execute import execute

    conn, client, ref = _funded_setup()
    result = execute(conn, client, ref.id, as_of="2026-11-03T12:00:00+00:00")
    assert result.remote_id == "awx-new" and len(client.created) == 1 and client.conversions == []
    assert execute(conn, client, ref.id, as_of="2026-11-03T12:00:00+00:00").remote_id == "awx-new"
    assert len(client.created) == 1

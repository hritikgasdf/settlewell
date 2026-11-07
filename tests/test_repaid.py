"""After a return, a colleague's payment closes the incident. No second replacement. No network."""

from decimal import Decimal

import pytest

from backend.airwallex.types import BalanceHistoryItem, BalanceHistoryPage, Transfer, TransferPage
from backend.ledger import connect, seed_hero
from backend.repaid import colleague_repaid
from backend.replace_payment import ReplaceError
from backend.replacement_tool import propose_replacement


def _transfer(transfer_id: str, status: str, request_id: str, **overrides) -> Transfer:
    fields = {
        "id": transfer_id,
        "status": status,
        "request_id": request_id,
        "transfer_amount": Decimal("4200.00"),
        "transfer_currency": "EUR",
        "reference": "INV-2291",
        "beneficiary_id": "ben_verified",
    }
    fields.update(overrides)
    return Transfer(**fields)


class Account:
    def __init__(self, items: tuple[Transfer, ...]) -> None:
        self.items = items
        self.created: list[dict] = []

    def list_transfers(self, **_kwargs):
        return TransferPage(items=self.items, page_after=None)

    def get_transfer(self, transfer_id: str) -> Transfer:
        for item in self.items:
            if item.id == transfer_id:
                return item
        return _transfer(transfer_id, "CANCELLED", "original-payment", beneficiary_id="ben_old")

    def balance_history(self, **_kwargs) -> BalanceHistoryPage:
        return BalanceHistoryPage(
            items=(
                BalanceHistoryItem(
                    id="rev-1",
                    source="awx-hero",
                    amount=Decimal("4200.00"),
                    currency="EUR",
                    balance=None,
                    description="Refund",
                    fee=Decimal(0),
                    posted_at=None,
                    source_type="PAYOUT",
                    transaction_type="PAYOUT_REVERSAL",
                    account_type=None,
                    reference="INV-2291",
                ),
            ),
            has_more=False,
            page_after=None,
        )

    def create_transfer(self, **kwargs):
        self.created.append(kwargs)
        raise AssertionError("a colleague payment must not be replaced")


def _returned(conn):
    incident_id = seed_hero(conn, airwallex_id="awx-hero")
    obligation_id = conn.execute(
        "SELECT obligation_id FROM incident WHERE id = ?",
        (incident_id,),
    ).fetchone()["obligation_id"]
    conn.execute(
        "UPDATE transfer_record SET status = 'CANCELLED', request_id = 'original-payment' WHERE airwallex_id = 'awx-hero'"
    )
    supplier_id = conn.execute(
        "SELECT supplier_id FROM obligation WHERE id = ?",
        (obligation_id,),
    ).fetchone()["supplier_id"]
    conn.execute(
        """
        INSERT INTO bank_detail_version (
            supplier_id, beneficiary_id, details_hash, bank_name, account_last4, source,
            verified_by, verified_at
        ) VALUES (?, 'ben_verified', 'callback-v2', 'Deutsche Kreditbank', '7781',
                  'verified_callback', 'finance', '2026-11-03T12:00:00+00:00')
        """,
        (supplier_id,),
    )
    conn.commit()
    return obligation_id, str(incident_id)


def test_a_colleague_payment_closes_the_incident_and_blocks_replacement():
    conn = connect()
    obligation_id, incident_ref = _returned(conn)
    colleague = _transfer("awx-colleague", "PAID", "colleague-hand")
    account = Account(
        (
            _transfer("awx-hero", "CANCELLED", "original-payment", beneficiary_id="ben_old"),
            colleague,
            _transfer("awx-other", "PAID", "other-invoice", reference="INV-9999"),
        )
    )

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    again = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "close"
    assert decision.airwallex_id == "awx-colleague"
    assert again.action == "close"
    incident = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()
    assert incident["state"] == "CLOSED"
    live = conn.execute(
        """
        SELECT transfer_record.airwallex_id, transfer_record.origin
        FROM obligation JOIN transfer_record ON transfer_record.id = obligation.live_transfer_id
        WHERE obligation.id = ?
        """,
        (obligation_id,),
    ).fetchone()
    assert live["airwallex_id"] == "awx-colleague"
    assert live["origin"] == "external"
    obligation = conn.execute("SELECT status FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    assert obligation["status"] == "PAID_ONCE"
    audits = conn.execute(
        "SELECT kind, detail FROM audit_event WHERE kind = 'colleague_repaid'"
    ).fetchall()
    assert len(audits) == 1
    assert audits[0]["detail"] == "awx-colleague"

    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code == "repaid"
    assert account.created == []


def test_a_scheduled_extra_offers_cancel_when_another_live_payment_exists():
    conn = connect()
    obligation_id, incident_ref = _returned(conn)
    account = Account(
        (
            _transfer("awx-hero", "CANCELLED", "original-payment"),
            _transfer("awx-paid", "PAID", "colleague-hand"),
            _transfer("awx-friday", "SCHEDULED", "colleague-friday"),
        )
    )

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "cancel"
    assert decision.airwallex_id == "awx-friday"
    incident = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()
    assert incident["state"] == "OPEN"
    stored = conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE origin = 'external' ORDER BY airwallex_id"
    ).fetchall()
    assert [row["airwallex_id"] for row in stored] == ["awx-friday", "awx-paid"]
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code == "repaid"
    assert account.created == []


class Pages(Account):
    def __init__(self, pages: dict) -> None:
        super().__init__(())
        self.pages = pages

    def list_transfers(self, **kwargs):
        return self.pages[kwargs.get("page") or "start"]


def test_a_wrong_supplier_on_an_early_page_does_not_close_on_a_later_known_payment():
    conn = connect()
    obligation_id, incident_ref = _returned(conn)
    pages = {
        "start": TransferPage(items=(_transfer("awx-stray", "PAID", "s", beneficiary_id="ben_other"),), page_after="2"),
        "2": TransferPage(items=(_transfer("awx-known", "PAID", "k"),), page_after=None),
    }

    decision = colleague_repaid(conn, Pages(pages), obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "ambiguous"
    state = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"]
    assert state == "OPEN"
    assert conn.execute("SELECT id FROM incident WHERE state = 'AMBIGUOUS'").fetchone() is not None
    assert conn.execute("SELECT id FROM audit_event WHERE kind = 'colleague_repaid'").fetchone() is None


def test_two_covered_known_payments_are_not_silently_chosen():
    conn = connect()
    obligation_id, incident_ref = _returned(conn)
    account = Account((_transfer("awx-a", "PAID", "a"), _transfer("awx-b", "PAID", "b")))

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "ambiguous"
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"


class Lifecycle(Account):
    """Account whose colleague payment status and reversal evidence are set per phase."""

    def __init__(self, colleague_status: str, *, reversal: bool = True) -> None:
        super().__init__(
            (
                _transfer("awx-hero", "CANCELLED", "original-payment", beneficiary_id="ben_old"),
                _transfer("awx-colleague", colleague_status, "colleague-hand"),
            )
        )
        self.reversal = reversal

    def balance_history(self, **kwargs):
        page = super().balance_history(**kwargs)
        return page if self.reversal else BalanceHistoryPage(items=(), has_more=False, page_after=None)


def _audit_kinds(conn):
    return [
        (row["kind"], row["detail"])
        for row in conn.execute(
            "SELECT kind, detail FROM audit_event WHERE kind IN "
            "('colleague_tracked','colleague_repaid','colleague_reopened',"
            "'original_disposition','replacement_disposition') ORDER BY id"
        )
    ]


def _live_airwallex_id(conn, obligation_id):
    return conn.execute(
        "SELECT t.airwallex_id FROM obligation o JOIN transfer_record t ON t.id = o.live_transfer_id WHERE o.id = ?",
        (obligation_id,),
    ).fetchone()["airwallex_id"]


@pytest.mark.parametrize("nonfinal", ["PROCESSING", "SENT", "OVERDUE"])
def test_a_nonfinal_colleague_payment_stays_open_then_closes_when_paid(tmp_path, nonfinal):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    obligation_id, incident_ref = _returned(conn)
    tracking = colleague_repaid(conn, Lifecycle(nonfinal), obligation_id=obligation_id, incident_ref=incident_ref)
    assert tracking.action == "track"
    assert tracking.airwallex_id == "awx-colleague"
    conn.close()

    for _ in range(2):  # repeated checks while nonfinal: still blocked, no new side effects
        conn = connect(path)
        account = Lifecycle(nonfinal)
        assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
        assert _live_airwallex_id(conn, obligation_id) == "awx-hero"
        with pytest.raises(ReplaceError) as caught:
            propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
        assert caught.value.code == "repaid"
        assert account.created == []
        again = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
        assert again.action == "track"
        assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
        assert _audit_kinds(conn) == [("colleague_tracked", "awx-colleague")]
        assert conn.execute("SELECT COUNT(*) AS n FROM incident").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM transfer_record").fetchone()["n"] == 2
        conn.close()

    conn = connect(path)
    paid = colleague_repaid(conn, Lifecycle("PAID"), obligation_id=obligation_id, incident_ref=incident_ref)
    assert paid.action == "close"
    assert paid.airwallex_id == "awx-colleague"
    conn.close()

    conn = connect(path)
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "CLOSED"
    assert _live_airwallex_id(conn, obligation_id) == "awx-colleague"
    stored = conn.execute("SELECT status FROM transfer_record WHERE airwallex_id = 'awx-colleague'").fetchone()
    assert stored["status"] == "PAID"
    assert conn.execute("SELECT status FROM obligation WHERE id = ?", (obligation_id,)).fetchone()["status"] == "PAID_ONCE"
    kinds = _audit_kinds(conn)
    assert ("original_disposition", "awx-hero:returned") in kinds
    assert ("replacement_disposition", "awx-colleague:PAID") in kinds
    assert ("colleague_repaid", "awx-colleague") in kinds
    again = colleague_repaid(conn, Lifecycle("PAID"), obligation_id=obligation_id, incident_ref=incident_ref)
    assert again.action == "close"
    assert _audit_kinds(conn) == kinds
    conn.close()


@pytest.mark.parametrize("nonfinal", ["PROCESSING", "SENT", "OVERDUE"])
@pytest.mark.parametrize("failed", ["FAILED", "CANCELLED"])
def test_a_nonfinal_colleague_payment_that_fails_reopens_and_allows_replacement(tmp_path, nonfinal, failed):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    obligation_id, incident_ref = _returned(conn)
    assert colleague_repaid(
        conn, Lifecycle(nonfinal), obligation_id=obligation_id, incident_ref=incident_ref
    ).action == "track"
    conn.close()

    conn = connect(path)
    account = Lifecycle(failed)
    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert decision.action == "replace"
    conn.close()

    conn = connect(path)
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
    assert _live_airwallex_id(conn, obligation_id) == "awx-hero"
    stored = conn.execute("SELECT status FROM transfer_record WHERE airwallex_id = 'awx-colleague'").fetchone()
    assert stored["status"] == failed
    kinds = _audit_kinds(conn)
    assert ("original_disposition", "awx-hero:returned") in kinds
    assert ("replacement_disposition", f"awx-colleague:{failed}") in kinds
    assert ("colleague_reopened", f"awx-colleague:{failed}") in kinds
    assert not any(kind == "colleague_repaid" for kind, _ in kinds)
    # The colleague gate no longer blocks. The next gate is the missing approval.
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code != "repaid"
    assert account.created == []
    assert colleague_repaid(
        conn, account, obligation_id=obligation_id, incident_ref=incident_ref
    ).action == "replace"
    assert _audit_kinds(conn) == kinds
    assert conn.execute("SELECT COUNT(*) AS n FROM incident").fetchone()["n"] == 1
    conn.close()


class Unlisted(Lifecycle):
    """The unfiltered discovery list omits the colleague payments; GET still has them."""

    def __init__(self, statuses: dict[str, str], *, reversal: bool = True) -> None:
        super().__init__("PROCESSING", reversal=reversal)
        self.items = (
            _transfer("awx-hero", "CANCELLED", "original-payment", beneficiary_id="ben_old"),
            *(_transfer(tid, status, f"req-{tid}") for tid, status in statuses.items()),
        )
        self.hidden = set(statuses)

    def list_transfers(self, **_kwargs):
        return TransferPage(items=tuple(i for i in self.items if i.id not in self.hidden), page_after=None)


def _track(conn, incident_ref, *ids):
    from backend.ledger import append_audit

    for tid in ids:
        append_audit(conn, incident_id=int(incident_ref), kind="colleague_tracked", detail=tid, created_at="t")
    conn.commit()


@pytest.mark.parametrize(
    ("status", "action"), [("PROCESSING", "track"), ("SENT", "track"), ("OVERDUE", "track"), ("PAID", "close")]
)
def test_a_tracked_payment_missing_from_the_list_is_resolved_by_get_after_restart(tmp_path, status, action):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    obligation_id, incident_ref = _returned(conn)
    assert colleague_repaid(
        conn, Lifecycle("PROCESSING"), obligation_id=obligation_id, incident_ref=incident_ref
    ).action == "track"
    conn.close()

    conn = connect(path)
    account = Unlisted({"awx-colleague": status})
    if action == "track":
        with pytest.raises(ReplaceError) as caught:
            propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
        assert caught.value.code == "repaid"
        assert account.created == []
    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert decision.action == action
    assert decision.airwallex_id == "awx-colleague"
    conn.close()

    conn = connect(path)
    state = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"]
    assert state == ("CLOSED" if action == "close" else "OPEN")
    if action == "close":
        assert _live_airwallex_id(conn, obligation_id) == "awx-colleague"
        assert ("replacement_disposition", "awx-colleague:PAID") in _audit_kinds(conn)
        with pytest.raises(ReplaceError) as caught:
            propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
        assert caught.value.code == "repaid"
    else:
        assert _live_airwallex_id(conn, obligation_id) == "awx-hero"
        assert not any(k in ("colleague_reopened", "colleague_repaid") for k, _ in _audit_kinds(conn))


@pytest.mark.parametrize("failed", ["FAILED", "CANCELLED"])
def test_a_tracked_payment_missing_from_the_list_that_failed_reopens(tmp_path, failed):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    obligation_id, incident_ref = _returned(conn)
    colleague_repaid(conn, Lifecycle("SENT"), obligation_id=obligation_id, incident_ref=incident_ref)
    conn.close()

    conn = connect(path)
    account = Unlisted({"awx-colleague": failed})
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code != "repaid"
    assert ("colleague_reopened", f"awx-colleague:{failed}") in _audit_kinds(conn)
    assert account.created == []


def test_one_final_failure_does_not_hide_another_live_tracked_payment(tmp_path):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    obligation_id, incident_ref = _returned(conn)
    _track(conn, incident_ref, "awx-a", "awx-b")
    account = Unlisted({"awx-a": "FAILED", "awx-b": "PROCESSING"})
    assert colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref).action == "track"
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code == "repaid"
    conn.close()

    conn = connect(path)
    kinds = _audit_kinds(conn)
    assert ("colleague_reopened", "awx-a:FAILED") in kinds
    assert not any(k == "colleague_reopened" and d.startswith("awx-b") for k, d in kinds)
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
    # The second one fails too: now, and only now, replacement is allowed.
    account = Unlisted({"awx-a": "FAILED", "awx-b": "CANCELLED"})
    assert colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref).action == "replace"
    assert ("colleague_reopened", "awx-b:CANCELLED") in _audit_kinds(conn)


def test_an_unknown_tracked_status_holds_even_when_another_tracked_payment_failed(tmp_path):
    conn = connect(tmp_path / "ledger.db")
    obligation_id, incident_ref = _returned(conn)
    _track(conn, incident_ref, "awx-a", "awx-b")
    account = Unlisted({"awx-a": "WEIRD", "awx-b": "FAILED"})

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "hold"
    assert ("colleague_reopened", "awx-b:FAILED") in _audit_kinds(conn)
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code == "repaid"


def test_a_tracked_colleague_payment_with_an_unknown_status_stays_blocked(tmp_path):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    obligation_id, incident_ref = _returned(conn)
    colleague_repaid(conn, Lifecycle("PROCESSING"), obligation_id=obligation_id, incident_ref=incident_ref)
    conn.close()

    conn = connect(path)
    decision = colleague_repaid(conn, Lifecycle("WEIRD"), obligation_id=obligation_id, incident_ref=incident_ref)
    assert decision.action == "hold"
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
    assert not any(kind in ("colleague_repaid", "colleague_reopened") for kind, _ in _audit_kinds(conn))


def test_a_paid_colleague_does_not_close_without_independent_return_evidence(tmp_path):
    conn = connect(tmp_path / "ledger.db")
    obligation_id, incident_ref = _returned(conn)

    decision = colleague_repaid(
        conn, Lifecycle("PAID", reversal=False), obligation_id=obligation_id, incident_ref=incident_ref
    )

    assert decision.action == "hold"
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
    assert _live_airwallex_id(conn, obligation_id) == "awx-hero"
    assert _audit_kinds(conn) == []


@pytest.mark.parametrize("failed", ["FAILED", "CANCELLED"])
def test_a_closed_colleague_payment_that_later_fails_reopens_on_a_direct_call_after_restart(tmp_path, failed):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    obligation_id, incident_ref = _returned(conn)
    assert colleague_repaid(conn, Lifecycle("PAID"), obligation_id=obligation_id, incident_ref=incident_ref).action == "close"
    conn.close()

    conn = connect(path)
    account = Lifecycle(failed)
    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert decision.action == "replace"
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
    assert conn.execute("SELECT status FROM obligation WHERE id = ?", (obligation_id,)).fetchone()["status"] != "PAID_ONCE"
    assert _live_airwallex_id(conn, obligation_id) == "awx-hero"
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code != "repaid"  # the next gate is the missing approval
    assert account.created == []
    conn.close()


@pytest.mark.parametrize("stale", ["SENT", "PROCESSING", "PAID"])
@pytest.mark.parametrize("failed", ["FAILED", "CANCELLED"])
def test_a_terminal_colleague_failure_outranks_later_stale_reads_across_restarts(tmp_path, failed, stale):
    path = tmp_path / "ledger.db"
    conn = connect(path)
    obligation_id, incident_ref = _returned(conn)
    conn.close()
    expected = [("PROCESSING", "track", "OPEN"), ("PAID", "close", "CLOSED"), (failed, "replace", "OPEN")]
    for status, action, state in expected:
        conn = connect(path)
        decision = colleague_repaid(conn, Lifecycle(status), obligation_id=obligation_id, incident_ref=incident_ref)
        assert (status, decision.action) == (status, action)
        assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == state
        conn.close()

    for status in (stale, "PAID"):  # stale reads after the terminal failure, each after a restart
        conn = connect(path)
        account = Lifecycle(status)
        decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
        assert decision.action not in ("close", "track")
        assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
        stored = conn.execute("SELECT status FROM transfer_record WHERE airwallex_id = 'awx-colleague'").fetchone()
        assert stored["status"] == failed
        assert conn.execute("SELECT status FROM obligation WHERE id = ?", (obligation_id,)).fetchone()["status"] != "PAID_ONCE"
        assert _live_airwallex_id(conn, obligation_id) == "awx-hero"
        with pytest.raises(ReplaceError) as caught:
            propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
        assert caught.value.code != "repaid"  # next gate is the missing approval
        assert account.created == []
        assert conn.execute("SELECT COUNT(*) AS n FROM incident").fetchone()["n"] == 1
        conn.close()


@pytest.mark.parametrize("visible_paid", [True, False])
def test_a_known_unpaid_extra_missing_from_the_list_blocks_close_and_replace(tmp_path, visible_paid):
    from backend.ledger import insert_external_transfer

    conn = connect(tmp_path / "ledger.db")
    obligation_id, incident_ref = _returned(conn)
    insert_external_transfer(  # discovered by an earlier poll, never colleague_tracked
        conn,
        airwallex_id="awx-extra",
        request_id="req-awx-extra",
        obligation_id=obligation_id,
        amount="4200.00",
        currency="EUR",
        status="SENT",
        reference="INV-2291",
    )
    conn.commit()
    account = Unlisted({"awx-extra": "PROCESSING"})
    if visible_paid:
        account.items = (*account.items, _transfer("awx-colleague", "PAID", "colleague-hand"))

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "hold"
    assert conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()["state"] == "OPEN"
    assert not any(kind == "colleague_repaid" for kind, _ in _audit_kinds(conn))
    with pytest.raises(ReplaceError) as caught:
        propose_replacement(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)
    assert caught.value.code == "repaid"
    assert account.created == []


def test_a_return_without_a_colleague_payment_still_allows_a_replacement_decision():
    conn = connect()
    obligation_id, incident_ref = _returned(conn)
    account = Account((_transfer("awx-hero", "CANCELLED", "original-payment"),))

    decision = colleague_repaid(conn, account, obligation_id=obligation_id, incident_ref=incident_ref)

    assert decision.action == "replace"
    assert decision.airwallex_id is None
    incident = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_ref,)).fetchone()
    assert incident["state"] == "OPEN"

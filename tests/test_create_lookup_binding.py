"""A timed-out or duplicate initial create may bind only the exact attempt's transfer. No network."""

import dataclasses
from decimal import Decimal

import pytest
from test_execute import SEEN, _grant, _ready
from test_funding_execute import Funded

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import Transfer, TransferPage
from backend.execute import execute
from backend.ledger import connect, migrate
from backend.replace_payment import ReplaceError

FAILURES = {
    "timeout": lambda: TimeoutError("slow"),
    "duplicate": lambda: AirwallexError(400, "duplicate_request_id", ""),
}

FOREIGN = {
    "request": ("request_id", "other-request"),
    "beneficiary": ("beneficiary_id", "ben_foreign"),
    "amount": ("transfer_amount", Decimal("4199.00")),
    "currency": ("transfer_currency", "USD"),
    "reference": ("reference", "INV-9999"),
    "nan": ("transfer_amount", Decimal("NaN")),
    "missing": ("beneficiary_id", None),
}
BAD = ["empty", "multiple", "cursor", *FOREIGN]


class Lookup(Funded):
    """create_transfer never returns; list_transfers answers per `variant`."""

    def __init__(self, failure, variant="ok"):
        super().__init__()
        self.failure = failure
        self.variant = variant

    def create_transfer(self, **kwargs):
        self.created.append(kwargs)
        raise FAILURES[self.failure]()

    def list_transfers(self, **_kwargs):
        if not self.created:  # a pre-send lookup finds nothing
            return TransferPage(items=(), page_after=None)
        kw = self.created[0]
        good = Transfer(
            id="awx-new", status="SCHEDULED", request_id=kw["request_id"], transfer_amount=kw["transfer_amount"],
            transfer_currency=kw["transfer_currency"], beneficiary_id=kw["beneficiary_id"], reference=kw["reference"],
        )
        v = self.variant
        if v == "ok":
            return TransferPage(items=(good,), page_after=None)
        if v == "empty":
            return TransferPage(items=(), page_after=None)
        if v == "multiple":
            return TransferPage(items=(good, dataclasses.replace(good, id="awx-other")), page_after=None)
        if v == "cursor":
            return TransferPage(items=(good,), page_after="next-page")
        field, value = FOREIGN[v]
        return TransferPage(items=(dataclasses.replace(good, **{field: value}),), page_after=None)


def _counts(conn):
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("execution_intent", "approval", "payout_funding", "transfer_record")
    }


def _funding(conn):
    return conn.execute("SELECT * FROM payout_funding").fetchall()


def _assert_unbound(conn, client, before):
    """Nothing about the foreign or ambiguous lookup reached the ledger."""
    rows = _funding(conn)
    assert len(rows) == 1
    row = rows[0]
    assert (row["state"], row["hold_state"], row["after_state"]) == ("PREFLIGHT", "HELD", "PENDING")
    assert row["actual_fee"] is None and row["actual_fee_currency"] is None
    assert row["request_id"] == client.created[0]["request_id"]
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM audit_event")}
    assert not kinds & {"payout_after_observed", "payout_after_missing", "payout_fee_over_budget"}
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshot WHERE label = 'after'").fetchone()[0] == 0
    own = conn.execute("SELECT airwallex_id FROM transfer_record WHERE request_id = ?", (row["request_id"],)).fetchall()
    assert [r["airwallex_id"] for r in own] == [None]
    foreign = conn.execute("SELECT COUNT(*) FROM transfer_record WHERE airwallex_id IN ('awx-new', 'awx-other')")
    assert foreign.fetchone()[0] == 0
    intent = conn.execute("SELECT remote_id, request_id FROM execution_intent").fetchone()
    assert intent["remote_id"] is None and intent["request_id"] == row["request_id"]
    assert len(client.created) == 1  # one create, one request id, no new attempt
    after = _counts(conn)
    assert after["execution_intent"] == before["execution_intent"]  # no other intent
    assert after["approval"] == before["approval"]  # no other grant
    assert after["payout_funding"] == 1
    assert after["transfer_record"] == before["transfer_record"] + 1  # only our durable attempt


@pytest.mark.parametrize("variant", BAD)
@pytest.mark.parametrize("failure", list(FAILURES))
def test_a_wrong_or_ambiguous_lookup_binds_nothing_and_never_resends(tmp_path, failure, variant):
    path = tmp_path / "x.db"
    conn = connect(path)
    migrate(conn)
    _o, incident_id = _ready(conn)
    ref = _grant(conn, incident_id)
    before = _counts(conn)
    client = Lookup(failure, variant)
    with pytest.raises(ReplaceError) as caught:
        execute(conn, client, ref.id, as_of=SEEN)
    assert caught.value.code == "unknown"
    assert conn.execute("SELECT status FROM obligation WHERE status = 'IN_FLIGHT'").fetchone() is not None
    _assert_unbound(conn, client, before)
    conn.close()

    # Restart while the lookup is still bad: nothing bound, no new POST.
    restarted = connect(path)
    with pytest.raises(ReplaceError):
        execute(restarted, client, ref.id, as_of=SEEN)
    _assert_unbound(restarted, client, before)
    restarted.close()

    # A later valid lookup recovers the same request once, without resend.
    client.variant = "ok"
    again = connect(path)
    assert execute(again, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert execute(again, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert len(client.created) == 1
    row = _funding(again)[0]
    assert (row["state"], row["hold_state"]) == ("ACCEPTED", "HELD")
    assert row["after_state"] in {"OBSERVED", "MISSING"}
    snaps = again.execute("SELECT COUNT(*) FROM balance_snapshot WHERE label = 'after'").fetchone()[0]
    missing = again.execute("SELECT COUNT(*) FROM audit_event WHERE kind = 'payout_after_missing'").fetchone()[0]
    assert (snaps == 1) if row["after_state"] == "OBSERVED" else (snaps == 0 and missing == 1)
    bound = again.execute("SELECT airwallex_id FROM transfer_record WHERE request_id = ?", (row["request_id"],))
    assert bound.fetchone()[0] == "awx-new"
    after = _counts(again)
    assert after["execution_intent"] == before["execution_intent"] and after["approval"] == before["approval"]


@pytest.mark.parametrize("failure", list(FAILURES))
def test_a_valid_lookup_after_a_timeout_or_duplicate_still_succeeds(failure):
    conn = connect()
    _o, incident_id = _ready(conn)
    ref = _grant(conn, incident_id)
    client = Lookup(failure, "ok")
    assert execute(conn, client, ref.id, as_of=SEEN).remote_id == "awx-new"
    assert len(client.created) == 1
    row = _funding(conn)[0]
    assert (row["state"], row["hold_state"]) == ("ACCEPTED", "HELD")

"""P2-8 live hero state machine against the stateful fake remote. No network, no credentials."""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.airwallex.client import AirwallexError
from backend.claims import mock_complete
from backend.hero_live import Hero, load_manifest
from backend.incident_remote import StatefulRemote
from backend.sandbox import SANDBOX_BASE, SandboxContractError, SandboxDriver


class Remote(StatefulRemote):
    _base_url = SANDBOX_BASE

    def __init__(self):
        super().__init__(eur="500.00", usd="100000.00")
        self.ben_posts = 0
        self.quote_valid_to = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()

    def create_beneficiary(self, body):
        self.ben_posts += 1
        return super().create_beneficiary(body)

    def post_json(self, path, body):  # the sandbox simulation endpoint
        tid = re.search(r"/transfers/([^/]+)/transition", path).group(1)
        if body["next_status"] == "FAILED":
            self.bounce(tid, "90701: Account closed")
        else:
            self.set_status(tid, body["next_status"])
        return {}


def _hero(tmp_path, remote):
    driver = SandboxDriver(remote, confirm_sandbox=True)
    return Hero(remote, driver, str(tmp_path / "m.json"), str(tmp_path / "run.db"),
                complete=mock_complete, sleep=lambda _s: None, polls=2)


def test_full_flow_through_execute_and_resume_does_not_repost(tmp_path):
    remote = Remote()
    result = _hero(tmp_path, remote).run()
    bad = {k: v for k, v in result.items() if not v.startswith(("PASS", "PARTIAL"))}
    assert not bad, bad
    manifest = load_manifest(str(tmp_path / "m.json"))
    assert manifest["stage"] == "close" and manifest["quotes"] and manifest["conversions"]
    assert set(manifest["ids"]) >= {"orig", "dup", "replacement", "new_ben", "old_ben"}
    posts, bens, convs = len(remote.posts), remote.ben_posts, len(remote.conversion_posts)
    again = _hero(tmp_path, remote).run()  # restart with the same manifest and DB
    assert again == result
    assert (len(remote.posts), remote.ben_posts, len(remote.conversion_posts)) == (posts, bens, convs)
    assert remote.duplicate_posts == 0


def test_crash_midway_resumes_without_new_posts(tmp_path):
    remote = Remote()
    hero = _hero(tmp_path, remote)
    hero._bootstrap()  # die right after the first stage, before 'stage' was saved
    before = (len(remote.posts), remote.ben_posts)
    result = _hero(tmp_path, remote).run()
    assert result["original_sent"].startswith("PASS"), result
    assert (len(remote.posts) - before[0], remote.ben_posts - before[1]) == (1, 1)  # replacement + new bank only


def test_runtime_failure_is_sanitized_not_faked(tmp_path):
    remote = Remote()

    def boom(**_k):
        raise AirwallexError(500, "internal_error", "secret body text")

    remote.create_transfer = boom
    result = _hero(tmp_path, remote).run()
    assert result["original_sent"].startswith("FAILED(AirwallexError:internal_error)")
    assert "secret" not in str(result)
    assert result["approved_replacement"] == "NOT_RUN"


def test_driver_creates_refuse_without_confirm_or_wrong_host():
    remote = Remote()
    with pytest.raises(SandboxContractError):
        SandboxDriver(remote).seed_world_transfer(transfer_amount=Decimal(1))
    remote._base_url = "https://api.airwallex.com"
    with pytest.raises(SandboxContractError):
        SandboxDriver(remote, confirm_sandbox=True).seed_world_beneficiary({})


def test_seed_scenario_world_refuses_outside_sandbox_before_any_remote_write(tmp_path):
    remote = Remote()
    remote._base_url = "https://api.airwallex.com"
    writes = []
    remote.create_beneficiary = lambda *a, **k: writes.append("ben")
    remote.create_transfer = lambda *a, **k: writes.append("transfer")
    for confirm, host in ((True, "https://api.airwallex.com"), (False, SANDBOX_BASE)):
        remote._base_url = host
        hero = Hero(remote, SandboxDriver(remote, confirm_sandbox=confirm), str(tmp_path / f"{confirm}.json"),
                    str(tmp_path / f"{confirm}.db"), complete=mock_complete, sleep=lambda _s: None)
        with pytest.raises(SandboxContractError):
            hero.seed_scenario_world()
        assert writes == [] and remote.posts == []


def test_reset_cancels_scheduled_duplicate_only_through_approved_execute(tmp_path, monkeypatch):
    import backend.scripts.hero_run as hr
    from backend.paid_once import scan_paid_once

    remote = Remote()
    hero = _hero(tmp_path, remote)
    hero._bootstrap()
    scan_paid_once(hero.conn, remote, int(hero.m["ids"]["obligation_row"]))
    hero.conn.close()
    raw = []
    real_cancel = remote.cancel_transfer
    monkeypatch.setattr(remote, "cancel_transfer", lambda tid, *a, **k: (raw.append(tid), real_cancel(tid, *a, **k))[1])
    import backend.execute as ex

    seen = []
    real_exec = ex.execute
    monkeypatch.setattr(ex, "execute", lambda c, cl, i, *, as_of: (seen.append(i), real_exec(c, cl, i, as_of=as_of))[1])
    ok, problems = hr.reset_owned(str(tmp_path / "m.json"), remote, SandboxDriver(remote, confirm_sandbox=True))
    assert ok, problems
    assert len(seen) == 1  # the cancel ran through execute (which is the only caller of the remote cancel)
    assert raw == [hero.m["ids"]["dup"]]  # remote cancel reached only from inside execute, for the owned dup


def test_hero_settlewell_actions_run_through_execute_with_bound_approvals(tmp_path, monkeypatch):
    import backend.hero_live as hl

    calls = []
    real = hl.execute

    def spy(conn, client, intent_id, *, as_of):
        row = conn.execute(
            "SELECT e.kind, e.approval_id, a.approver, a.proposer FROM execution_intent e "
            "JOIN approval a ON a.id = e.approval_id WHERE e.id = ?", (intent_id,)).fetchone()
        calls.append((row["kind"], bool(row["approver"]), row["approver"] != row["proposer"]))  # approved by someone else
        return real(conn, client, intent_id, as_of=as_of)

    monkeypatch.setattr(hl, "execute", spy)
    remote = Remote()
    seeds = []
    hero = _hero(tmp_path, remote)
    for name in ("seed_world_beneficiary", "seed_world_transfer"):
        orig = getattr(hero.driver, name)
        monkeypatch.setattr(hero.driver, name, lambda *a, _o=orig, _n=name, **k: (seeds.append(_n), _o(*a, **k))[1])
    result = hero.run()
    assert all(v.startswith(("PASS", "PARTIAL")) for v in result.values()), result
    kinds = [c[0] for c in calls]
    assert {"cancel", "beneficiary", "replacement"} <= set(kinds), kinds
    assert all(approved and different for _k, approved, different in calls), calls
    # Only the pre-existing world is seeded: old beneficiary, original, duplicate. Nothing else is created directly.
    assert seeds == ["seed_world_beneficiary", "seed_world_transfer", "seed_world_transfer"], seeds
    assert remote.ben_posts == 2  # old (seed) + new bank (execute)


def _iban_mod97(iban: str) -> bool:
    s = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


def test_new_bank_fixture_is_valid_distinct_dkb_iban():
    from backend.hero_live import NEW_BANK, OLD_BANK

    new = NEW_BANK["beneficiary"]["bank_details"]
    old = OLD_BANK["beneficiary"]["bank_details"]
    assert _iban_mod97(new["iban"]) and _iban_mod97(old["iban"])
    assert new["iban"].startswith("DE") and len(new["iban"]) == 22
    assert new["bank_country_code"] == "DE"
    assert new["iban"][4:12] == "12030000"  # DKB routing (BLZ)
    assert new["swift_code"].startswith("BYLADEM")
    assert new["iban"] != old["iban"] and new["swift_code"] != old["swift_code"]


def test_hero_binds_last4_to_new_iban():
    import inspect

    from backend import hero_live

    src = inspect.getsource(hero_live)
    assert 'account_last4="7781"' not in src
    assert 'NEW_BANK["beneficiary"]["bank_details"]["iban"][-4:]' in src
    assert hero_live.NEW_BANK["beneficiary"]["bank_details"]["iban"][-4:] == "2051"


def test_original_and_duplicate_fixtures_share_eur_source_currency(tmp_path):
    """Controlled fixture: no incidental FX; the full-principal refund currency matches the transfer."""
    remote = Remote()
    _hero(tmp_path, remote).run()
    ids = load_manifest(str(tmp_path / "m.json"))["ids"]
    orig, dup = remote.transfers[ids["orig"]], remote.transfers[ids["dup"]]
    assert orig.transfer_currency == dup.transfer_currency == "EUR"
    assert orig.transfer_amount == dup.transfer_amount
    sent = [p for p in remote.posts if p.get("request_id") in (orig.request_id, dup.request_id)]
    assert len(sent) == 2
    assert {(p["transfer_currency"], p["source_currency"]) for p in sent} == {("EUR", "EUR")}


def _stored_claim(tmp_path):
    import json
    import sqlite3

    conn = sqlite3.connect(tmp_path / "run.db")
    try:
        return json.loads(conn.execute("SELECT claim_json FROM incident WHERE id = 1").fetchone()[0])
    finally:
        conn.close()


def test_claim_records_safe_parse_provenance_for_an_injected_parser(tmp_path):
    _hero(tmp_path, Remote()).run()
    claim = _stored_claim(tmp_path)
    assert claim["matched"] is True and claim["invoice_ref"].startswith("INV-")
    assert claim["parse"]["source"] == "injected_test_parser" and claim["parse"]["model"] is None


def test_claim_records_claude_model_name_without_the_raw_reply(tmp_path, monkeypatch):
    from backend import claims

    reply = {}

    def fake_meta(email):
        reply["text"] = claims.mock_complete(email)
        return reply["text"], {"model": "claude-haiku-4-5", "input_tokens": 9, "output_tokens": 9}

    monkeypatch.setattr(claims, "complete_with_meta", fake_meta)
    remote = Remote()
    hero = Hero(remote, SandboxDriver(remote, confirm_sandbox=True), str(tmp_path / "m.json"),
                str(tmp_path / "run.db"), complete=claims.anthropic_complete, sleep=lambda _s: None, polls=2)
    hero.run()
    claim = _stored_claim(tmp_path)
    assert claim["parse"]["source"] == "claude" and claim["parse"]["model"] == "claude-haiku-4-5"
    assert reply["text"] not in json.dumps(claim)

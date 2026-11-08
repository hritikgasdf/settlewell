"""P2-8 live hero state machine against the stateful fake remote. No network, no credentials."""

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
        SandboxDriver(remote).create_owned_transfer(transfer_amount=Decimal(1))
    remote._base_url = "https://api.airwallex.com"
    with pytest.raises(SandboxContractError):
        SandboxDriver(remote, confirm_sandbox=True).create_owned_beneficiary({})


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

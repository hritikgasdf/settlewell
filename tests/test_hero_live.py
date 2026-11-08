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

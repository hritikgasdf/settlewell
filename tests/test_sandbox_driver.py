"""Sandbox driver. These tests do not call Airwallex."""

from decimal import Decimal
from pathlib import Path

import pytest

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import (
    BalanceHistoryItem,
    BalanceHistoryPage,
    ConfirmationLetter,
    parse_transfer,
)
from backend.sandbox import SandboxContractError, SandboxDriver, _live_contract, run_contract_cli
from backend.scripts.sandbox_contract import run_closed_return


class ScriptedApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.transfer = {
            "id": "tr_contract",
            "status": "SCHEDULED",
            "request_id": "stl-contract-1",
            "transfer_currency": "USD",
            "transfer_amount": "20",
        }

    def create_transfer(self, **_kwargs):
        self.calls.append(("create",))
        return parse_transfer(self.transfer)

    def post_json(self, path, body):
        self.calls.append((path, body["next_status"], body.get("failure_type")))
        if body["next_status"] == "SENT":
            self.transfer = {**self.transfer, "status": "SENT", "dispatch_date": "2026-11-03"}
        if body["next_status"] == "FAILED":
            self.transfer = {
                **self.transfer,
                "status": "CANCELLED",
                "failure_reason": "90701: Account closed",
                "failure_type": "INCORRECT_ROUTING",
            }
        return dict(self.transfer)

    def get_transfer(self, transfer_id):
        self.calls.append(("get", transfer_id))
        return parse_transfer(self.transfer)

    def create_confirmation_letter(self, **_kwargs):
        return ConfirmationLetter(content=b"%PDF-1.4" + b"x" * 200, media_type="application/pdf")

    def balance_history(self, **_kwargs):
        item = BalanceHistoryItem(
            id="h1",
            source="tr_contract",
            amount=Decimal(20),
            currency="USD",
            balance=None,
            description=None,
            fee=None,
            posted_at=None,
            source_type="PAYOUT",
            transaction_type="PAYOUT_REVERSAL",
            account_type=None,
            reference=None,
        )
        return BalanceHistoryPage(items=(item,), has_more=False, page_after=None)


def test_account_closed_contract_reads_reason_letter_and_reversal():
    api = ScriptedApi()
    result = run_closed_return(
        api,
        transfer_amount=Decimal(20),
        transfer_currency="USD",
        source_currency="USD",
        beneficiary_id="ben_1",
        transfer_method="LOCAL",
        reference="SW-CONTRACT",
        reason="other_services",
        request_id="stl-contract-1",
    )

    assert result.saw_sent is True
    assert result.final_status == "CANCELLED"
    assert result.reason_on_get is True
    assert result.letter_bytes > 100
    assert result.reversal_joined is True
    assert result.passed is True
    sent = ("/api/v1/simulation/transfers/tr_contract/transition", "SENT", None)
    failed = ("/api/v1/simulation/transfers/tr_contract/transition", "FAILED", "ACCOUNT_CLOSED")
    assert sent in api.calls
    assert failed in api.calls
    assert api.calls.index(sent) < api.calls.index(failed)
    assert ("get", "tr_contract") in api.calls


def test_transition_reads_back_when_the_simulation_call_fails():
    class Falling:
        def post_json(self, path, body):
            raise AirwallexError(500, "operation_failed", "")

        def get_transfer(self, transfer_id):
            return parse_transfer({"id": transfer_id, "status": "SENT"})

    seen = SandboxDriver(Falling()).transition("tr_1", "SENT")
    assert seen.status == "SENT"


def test_simulation_path_lives_only_in_the_driver():
    package = Path(__file__).resolve().parents[1] / "backend"
    adapter = "\n".join(path.read_text() for path in (package / "airwallex").rglob("*.py"))
    driver = (package / "sandbox.py").read_text()
    assert "simulation" not in adapter.lower()
    assert "/api/v1/simulation/transfers/" in driver


def test_contract_creates_refuse_outside_the_script():
    api = ScriptedApi()
    with pytest.raises(SandboxContractError):
        SandboxDriver(api).prove_account_closed_return(
            transfer_amount=Decimal(20),
            transfer_currency="USD",
            source_currency="USD",
            beneficiary_id="ben_1",
            transfer_method="LOCAL",
            reference="SW-CONTRACT",
            reason="other_services",
            request_id="stl-contract-1",
        )
    assert api.calls == []
    with pytest.raises(SandboxContractError):
        _live_contract()


def test_contract_cli_does_not_call_the_network_by_default(monkeypatch, capsys):
    monkeypatch.delenv("SETTLEWELL_SANDBOX", raising=False)
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    assert run_contract_cli() == 0
    assert capsys.readouterr().out.strip() == "contract skipped"


def test_contract_cli_mock_makes_no_api_call(monkeypatch, capsys):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    monkeypatch.delenv("SETTLEWELL_SANDBOX", raising=False)
    assert run_contract_cli() == 0
    assert capsys.readouterr().out.strip() == "MOCK"


class FeeUnknownCreate(ScriptedApi):
    """Remote accepted the create (transfer exists by request_id) but answered 400 fee_unknown."""

    def __init__(self, accepted: bool) -> None:
        super().__init__()
        self.accepted = accepted
        self.creates = 0

    def create_transfer(self, **_kwargs):
        self.creates += 1
        raise AirwallexError(400, "fee_unknown", "")

    def list_transfers(self, **_kwargs):
        from types import SimpleNamespace

        items = (parse_transfer(self.transfer),) if self.accepted else ()
        return SimpleNamespace(items=items, page_after=None)


_ARGS = {
    "transfer_amount": Decimal(20), "transfer_currency": "USD", "source_currency": "USD",
    "beneficiary_id": "ben_1", "transfer_method": "LOCAL", "reference": "SW-CONTRACT",
    "reason": "other_services", "request_id": "stl-contract-1",
}


def test_create_error_with_accepted_object_recovers_by_request_id_without_second_create():
    api = FeeUnknownCreate(accepted=True)
    result = run_closed_return(api, **_ARGS)
    assert api.creates == 1 and result.passed


def test_create_error_not_accepted_fails_closed_with_stage_and_request_id():
    from backend import sandbox

    api = FeeUnknownCreate(accepted=False)
    with pytest.raises(AirwallexError):
        run_closed_return(api, **_ARGS)
    assert api.creates == 1  # never re-created
    assert sandbox.LAST_TRACE["stage"] == "create_transfer"
    assert sandbox.LAST_TRACE["request_id"] == "stl-contract-1"
    assert sandbox.LAST_TRACE["accepted"] == "no"


class SettlingApi:
    """A new transfer answers 500 to a SENT simulation until it auto-moves to PROCESSING."""

    def __init__(self, scheduled_polls: int) -> None:
        self.status, self.left, self.posts = "SCHEDULED", scheduled_polls, 0

    def post_json(self, _path, body):
        self.posts += 1
        if self.status == "SCHEDULED":
            self.left -= 1
            if self.left <= 0:
                self.status = "PROCESSING"
            raise AirwallexError(500, "operation_failed", "simulation")
        self.status = body["next_status"]
        return {"id": "tr_new", "status": self.status}

    def get_transfer(self, _transfer_id):
        return parse_transfer({"id": "tr_new", "status": self.status})


def test_reach_retries_transition_once_transfer_leaves_scheduled():
    api = SettlingApi(scheduled_polls=2)
    result = SandboxDriver(api, confirm_sandbox=True)._reach("tr_new", "SENT")
    assert result.status == "SENT"
    assert api.posts == 3  # two refused while SCHEDULED, then the PROCESSING -> SENT move


def test_reach_gives_up_after_attempts():
    api = SettlingApi(scheduled_polls=100)
    result = SandboxDriver(api, attempts=3, confirm_sandbox=True)._reach("tr_new", "SENT")
    assert result.status == "SCHEDULED" and api.posts == 4

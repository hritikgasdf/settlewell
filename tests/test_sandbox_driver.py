"""Sandbox driver. These tests do not call Airwallex."""

from decimal import Decimal
from pathlib import Path

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import (
    BalanceHistoryItem,
    BalanceHistoryPage,
    ConfirmationLetter,
    parse_transfer,
)
from backend.sandbox import SandboxDriver, run_contract_cli


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
    result = SandboxDriver(api).prove_account_closed_return(
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

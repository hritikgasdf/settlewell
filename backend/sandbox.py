"""Sandbox driver. This is the only place that calls Airwallex simulation.

`prove_account_closed_return` creates a transfer, and `_live_contract` creates a
beneficiary, only so the contract script can manufacture a failed payout. Those
two calls are not a payment path and they do not approve anything. They raise
unless the caller is `backend.scripts.sandbox_contract`.
"""

from __future__ import annotations

import inspect
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from backend.airwallex.client import AirwallexClient, AirwallexError
from backend.airwallex.types import Transfer, parse_transfer
from settlewell.flags import mock_mode, sandbox_enabled

Sleeper = Callable[[float], None]

CONTRACT_BENEFICIARY = {
    "beneficiary": {
        "address": {
            "city": "New York",
            "country_code": "US",
            "postcode": "10001",
            "state": "NY",
            "street_address": "1 Synthetic Avenue",
        },
        "bank_details": {
            "account_currency": "USD",
            "account_name": "Lumen Textiles GmbH",
            "account_number": "50001121",
            "account_routing_type1": "aba",
            "account_routing_value1": "021000021",
            "bank_account_category": "Checking",
            "bank_country_code": "US",
            "local_clearing_system": "ACH",
        },
        "company_name": "Lumen Textiles GmbH",
        "entity_type": "COMPANY",
        "type": "BANK_ACCOUNT",
    },
    "nickname": "settlewell-contract-usd",
    "transfer_methods": ["LOCAL"],
}


@dataclass(frozen=True)
class ContractResult:
    saw_sent: bool
    final_status: str
    reason_on_get: bool
    letter_bytes: int
    reversal_joined: bool

    @property
    def passed(self) -> bool:
        return (
            self.saw_sent
            and self.final_status == "CANCELLED"
            and self.reason_on_get
            and self.letter_bytes > 100
            and self.reversal_joined
        )


_SENT_PAGE_SIZE = 10
_SENT_PAGE_LIMIT = 5


class SandboxContractError(Exception):
    """A contract-only Airwallex create was called from outside the contract script."""

    def __init__(self) -> None:
        super().__init__("contract_script")


def _require_contract_script() -> None:
    for frame in inspect.stack():
        name = frame.frame.f_globals.get("__name__", "")
        if name == "backend.scripts.sandbox_contract":
            return
        if frame.filename.replace("\\", "/").endswith("backend/scripts/sandbox_contract.py"):
            return
    raise SandboxContractError()


# Safe contract trace (stage, request id, object id, accepted yes/no/unknown). No bodies or credentials.
LAST_TRACE: dict[str, str] = {}

SANDBOX_BASE = "https://api-demo.airwallex.com"
HERO_MODULES = ("backend.hero_live", "backend.scripts.hero_run")


class SandboxDriver:
    def __init__(self, client, *, sleep: Sleeper | None = None, attempts: int = 8,
                 confirm_sandbox: bool = False) -> None:
        self._client = client
        self._sleep = sleep or _noop
        self._attempts = attempts
        self._confirm = confirm_sandbox

    def _require_hero(self) -> None:
        """Owned fixtures only: explicit confirm, the exact sandbox host, an approved entrypoint."""
        if not self._confirm or getattr(self._client, "_base_url", None) != SANDBOX_BASE:
            raise SandboxContractError()
        for frame in inspect.stack():
            if frame.frame.f_globals.get("__name__", "") in HERO_MODULES:
                return
        raise SandboxContractError()

    def create_owned_beneficiary(self, body):
        self._require_hero()
        return self._client.create_beneficiary(body)

    def create_owned_transfer(self, **kwargs) -> Transfer:
        self._require_hero()
        return self._client.create_transfer(**kwargs)

    def reach(self, transfer_id: str, status: str) -> Transfer:
        self._require_hero()
        return self._reach(transfer_id, status)

    def fail_account_closed(self, transfer_id: str) -> Transfer:
        """Simulate the bank return on an owned SENT transfer; poll until CANCELLED/90701."""
        self._require_hero()
        self.transition(transfer_id, "FAILED", "ACCOUNT_CLOSED")
        return self._until(transfer_id, _account_closed)

    def reversal_joined(self, transfer_id: str, currency: str) -> bool:
        return self._reversal_joined(transfer_id, currency)

    def list_remote_sent(self) -> list[str]:
        """List sandbox transfers at SENT. Stops after a fixed number of pages."""
        found: list[str] = []
        page = None
        for _ in range(_SENT_PAGE_LIMIT):
            batch = self._client.list_transfers(page_size=_SENT_PAGE_SIZE, page=page)
            for item in batch.items:
                if item.status == "SENT" and item.id and not str(item.id).startswith("synthetic"):
                    found.append(item.id)
            page = batch.page_after
            if not page:
                break
        return found

    def release_sent(self, transfer_id: str) -> Transfer:
        """Move a sandbox transfer off SENT. A transfer that is not SENT is left alone."""
        current = self._client.get_transfer(transfer_id)
        if current.status != "SENT":
            return current
        return self.transition(transfer_id, "PAID")

    def transition(
        self,
        transfer_id: str,
        next_status: str,
        failure_type: str | None = None,
    ) -> Transfer:
        body: dict[str, str] = {"next_status": next_status}
        if failure_type:
            body["failure_type"] = failure_type
        safe_id = urllib.parse.quote(transfer_id, safe="")
        path = f"/api/v1/simulation/transfers/{safe_id}/transition"
        try:
            payload = self._client.post_json(path, body)
        except AirwallexError:
            return self._client.get_transfer(transfer_id)
        if payload.get("id") and payload.get("status"):
            return parse_transfer(payload)
        return self._client.get_transfer(transfer_id)

    def prove_account_closed_return(
        self,
        *,
        transfer_amount: Decimal,
        transfer_currency: str,
        source_currency: str,
        beneficiary_id: str,
        transfer_method: str,
        reference: str,
        reason: str,
        request_id: str,
    ) -> ContractResult:
        _require_contract_script()
        LAST_TRACE.clear()
        LAST_TRACE.update(stage="create_transfer", request_id=request_id, accepted="unknown")
        try:
            created = self._client.create_transfer(
                transfer_amount=transfer_amount,
                transfer_currency=transfer_currency,
                source_currency=source_currency,
                beneficiary_id=beneficiary_id,
                transfer_method=transfer_method,
                reference=reference,
                reason=reason,
                request_id=request_id,
            )
        except AirwallexError:
            created = self._recover_created(request_id)  # never a second create
            if created is None:
                raise
        LAST_TRACE.update(accepted="yes", transfer_id=created.id, stage="reach_sent")
        sent = self._reach(created.id, "SENT")
        try:
            self.transition(created.id, "FAILED", "ACCOUNT_CLOSED")
            final = self._until(created.id, _account_closed)
        except AirwallexError:
            if sent.status == "SENT":
                self._try_fail(created.id)
            raise
        letter = self._client.create_confirmation_letter(
            transaction_id=created.id,
            letter_format="NO_FEE_DISPLAY",
        )
        return ContractResult(
            saw_sent=sent.status == "SENT",
            final_status=final.status,
            reason_on_get=_account_closed(final),
            letter_bytes=len(letter.content),
            reversal_joined=self._reversal_joined(created.id, transfer_currency),
        )

    def _recover_created(self, request_id: str):
        """After an error on create, read by our request id. Exact match only; unreadable stays unknown."""
        try:
            items = self._client.list_transfers(request_id=request_id).items
        except AirwallexError:
            return None
        found = next((t for t in items if t.request_id == request_id and t.id), None)
        LAST_TRACE["accepted"] = "yes" if found else "no"
        return found

    def _reach(self, transfer_id: str, status: str) -> Transfer:
        current = self.transition(transfer_id, status)
        if current.status == status:
            return current
        return self._until(transfer_id, lambda transfer: transfer.status == status)

    def _until(self, transfer_id: str, predicate) -> Transfer:
        last = self._client.get_transfer(transfer_id)
        if predicate(last):
            return last
        for _ in range(self._attempts):
            self._sleep(0.5)
            last = self._client.get_transfer(transfer_id)
            if predicate(last):
                return last
        return last

    def _try_fail(self, transfer_id: str) -> None:
        try:
            self.transition(transfer_id, "FAILED", "ACCOUNT_CLOSED")
        except AirwallexError:
            return

    def _reversal_joined(self, transfer_id: str, currency: str) -> bool:
        page = None
        for _ in range(self._attempts):
            history = self._client.balance_history(currency=currency, page_size=10, page=page)
            for item in history.items:
                if item.transaction_type == "PAYOUT_REVERSAL" and item.source == transfer_id:
                    return True
            if history.has_more and history.page_after and history.page_after != page:
                page = history.page_after
                continue
            page = None
            self._sleep(0.5)
        return False


def run_contract_cli() -> int:
    """Live contract when SETTLEWELL_SANDBOX=1. Otherwise print and make no API call."""
    if mock_mode():
        print("MOCK")
        return 0
    if not sandbox_enabled():
        print("contract skipped")
        return 0
    try:
        result = _live_contract()
    except AirwallexError as error:
        print("contract_error_status", error.status_code)
        print("contract_error_code", error.code or "none")
        for key in ("stage", "request_id", "transfer_id", "accepted"):
            if LAST_TRACE.get(key):
                print("contract_trace", key, LAST_TRACE[key])
        print("contract fail")
        return 1
    print("saw_sent", "pass" if result.saw_sent else "fail")
    print("final_status", result.final_status)
    print("reason_on_get", "pass" if result.reason_on_get else "fail")
    print("letter", "pass" if result.letter_bytes > 100 else "fail")
    print("reversal_join", "pass" if result.reversal_joined else "fail")
    print("contract", "pass" if result.passed else "fail")
    return 0 if result.passed else 1


def _live_contract() -> ContractResult:
    _require_contract_script()
    client = AirwallexClient.from_env()
    validation = client.validate_beneficiary(CONTRACT_BENEFICIARY)
    print("validate_beneficiary", "pass" if validation.ok else "fail")
    if not validation.ok:
        return ContractResult(False, "", False, 0, False)
    beneficiary = client.create_beneficiary(CONTRACT_BENEFICIARY)
    print("create_beneficiary", "pass" if beneficiary.id else "fail")
    return SandboxDriver(client, sleep=_sleep).prove_account_closed_return(
        transfer_amount=Decimal(20),
        transfer_currency="USD",
        source_currency="USD",
        beneficiary_id=beneficiary.id,
        transfer_method="LOCAL",
        reference="SW-CONTRACT",
        reason="other_services",
        request_id="stl-c-" + uuid.uuid4().hex[:16],
    )


def _account_closed(transfer: Transfer) -> bool:
    return transfer.status == "CANCELLED" and bool(
        transfer.failure_reason and transfer.failure_reason.startswith("90701")
    )


def _noop(_seconds: float) -> None:
    return None


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)

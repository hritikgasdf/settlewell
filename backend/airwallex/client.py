"""Airwallex sandbox adapter.

Callers use typed methods. Login, the pinned API version, the bearer token,
and rate limiting stay inside this client.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from backend.airwallex.http import HttpRequest, HttpResponse, UrllibTransport
from backend.airwallex.types import (
    Balance,
    BalanceHistoryPage,
    Beneficiary,
    BeneficiaryValidation,
    CancelResult,
    ConfirmationLetter,
    Conversion,
    FxQuote,
    Transfer,
    TransferPage,
    parse_balance,
    parse_history_item,
    parse_quote,
    parse_transfer,
)

API_VERSION = "2025-11-11"
DEFAULT_BASE_URL = "https://api-demo.airwallex.com"
MIN_INTERVAL_SECONDS = 0.2
REFRESH_MARGIN_SECONDS = 60
MAX_RATE_LIMIT_RETRIES = 3

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


class AirwallexError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str | None,
        message: str,
        field_codes: tuple[str, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field_codes = field_codes
        super().__init__(f"HTTP {status_code} {code or ''}".rstrip())


class MissingCredentials(Exception):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("Missing " + ", ".join(missing))


class AirwallexClient:
    def __init__(
        self,
        *,
        client_id: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        transport=None,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self._client_id = client_id
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport or UrllibTransport()
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self._lock = threading.RLock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._next_ms = 0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, transport=None) -> AirwallexClient:
        source = os.environ if env is None else env
        required = ("AIRWALLEX_SANDBOX_CLIENT_ID", "AIRWALLEX_SANDBOX_API_KEY")
        missing = tuple(name for name in required if not source.get(name))
        if missing:
            raise MissingCredentials(missing)
        return cls(
            client_id=source["AIRWALLEX_SANDBOX_CLIENT_ID"],
            api_key=source["AIRWALLEX_SANDBOX_API_KEY"],
            transport=transport,
        )

    def current_balances(self) -> list[Balance]:
        payload = self._json("GET", "/api/v1/balances/current")
        rows = payload if isinstance(payload, list) else []
        return [parse_balance(row) for row in rows]

    def balance_history(
        self,
        *,
        currency: str,
        page_size: int = 10,
        page: str | None = None,
    ) -> BalanceHistoryPage:
        payload = self._json(
            "GET",
            "/api/v1/balances/history",
            query=[("currency", currency), ("page_size", page_size), ("page", page)],
        )
        data = payload if isinstance(payload, dict) else {}
        items = tuple(parse_history_item(row) for row in data.get("items") or [])
        return BalanceHistoryPage(
            items=items,
            has_more=bool(data.get("has_more")),
            page_after=_optional_str(data.get("page_after")),
        )

    def list_transfers(
        self,
        *,
        request_id: str | None = None,
        short_reference_id: str | None = None,
        page_size: int | None = None,
        page: str | None = None,
    ) -> TransferPage:
        payload = self._json(
            "GET",
            "/api/v1/transfers",
            query=[
                ("request_id", request_id),
                ("short_reference_id", short_reference_id),
                ("page_size", page_size),
                ("page", page),
            ],
        )
        data = payload if isinstance(payload, dict) else {}
        return TransferPage(
            items=tuple(parse_transfer(row) for row in data.get("items") or []),
            page_after=_optional_str(data.get("page_after")),
        )

    def get_transfer(self, transfer_id: str) -> Transfer:
        payload = self._json("GET", f"/api/v1/transfers/{_path_id(transfer_id)}")
        return parse_transfer(payload)

    def create_transfer(
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
        transfer_date: str | None = None,
        metadata: dict | None = None,
    ) -> Transfer:
        body: dict[str, Any] = {
            "transfer_amount": transfer_amount,
            "transfer_currency": transfer_currency,
            "source_currency": source_currency,
            "beneficiary_id": beneficiary_id,
            "transfer_method": transfer_method,
            "reference": reference,
            "reason": reason,
            "request_id": request_id,
        }
        if transfer_date is not None:
            body["transfer_date"] = transfer_date
        if metadata is not None:
            body["metadata"] = metadata
        return parse_transfer(self._json("POST", "/api/v1/transfers/create", json_body=body))

    def cancel_transfer(self, transfer_id: str) -> CancelResult:
        payload = self._json("POST", f"/api/v1/transfers/{_path_id(transfer_id)}/cancel", json_body={})
        if payload == "OK":
            return CancelResult(ok=True, transfer=None)
        if isinstance(payload, dict) and payload.get("id"):
            return CancelResult(ok=True, transfer=parse_transfer(payload))
        return CancelResult(ok=True, transfer=None)

    def validate_beneficiary(self, body: Mapping) -> BeneficiaryValidation:
        try:
            payload = self._json("POST", "/api/v1/beneficiaries/validate", json_body=dict(body))
        except AirwallexError as error:
            if error.code == "validation_failed":
                return BeneficiaryValidation(ok=False, field_codes=error.field_codes)
            raise
        return BeneficiaryValidation(ok=payload == "OK" or payload is True)

    def create_beneficiary(self, body: Mapping) -> Beneficiary:
        payload = self._json("POST", "/api/v1/beneficiaries/create", json_body=dict(body))
        data = payload if isinstance(payload, dict) else {}
        return Beneficiary(id=str(data["id"]), nickname=_optional_str(data.get("nickname")))

    def create_confirmation_letter(self, *, transaction_id: str, letter_format: str) -> ConfirmationLetter:
        response = self._send(
            "POST",
            "/api/v1/confirmation_letters/create",
            json_body={"format": letter_format, "transaction_id": transaction_id},
            accept="*/*",
        )
        media_type = response.headers.get("Content-Type") or response.headers.get("content-type") or "application/pdf"
        return ConfirmationLetter(content=response.body, media_type=media_type.split(";")[0].strip())

    def create_quote(
        self,
        *,
        buy_currency: str,
        sell_currency: str,
        validity: str,
        buy_amount: Decimal | None = None,
        sell_amount: Decimal | None = None,
    ) -> FxQuote:
        body: dict[str, Any] = {
            "buy_currency": buy_currency,
            "sell_currency": sell_currency,
            "validity": validity,
        }
        if buy_amount is not None:
            body["buy_amount"] = buy_amount
        if sell_amount is not None:
            body["sell_amount"] = sell_amount
        return parse_quote(self._json("POST", "/api/v1/fx/quotes/create", json_body=body))

    def get_quote(self, quote_id: str) -> FxQuote:
        return parse_quote(self._json("GET", f"/api/v1/fx/quotes/{_path_id(quote_id)}"))

    def create_conversion(
        self,
        *,
        quote_id: str,
        buy_currency: str,
        sell_currency: str,
        buy_amount: Decimal | None = None,
        sell_amount: Decimal | None = None,
        request_id: str | None = None,
    ) -> Conversion:
        body: dict[str, Any] = {
            "quote_id": quote_id,
            "buy_currency": buy_currency,
            "sell_currency": sell_currency,
        }
        if buy_amount is not None:
            body["buy_amount"] = buy_amount
        if sell_amount is not None:
            body["sell_amount"] = sell_amount
        if request_id is not None:
            body["request_id"] = request_id
        payload = self._json("POST", "/api/v1/fx/conversions/create", json_body=body)
        data = payload if isinstance(payload, dict) else {}
        return Conversion(
            conversion_id=str(data.get("conversion_id") or data.get("id")),
            status=_optional_str(data.get("status")),
            quote_id=_optional_str(data.get("quote_id")),
        )

    def post_json(self, path: str, body: Mapping) -> dict:
        """Authenticated POST. The caller supplies the path; headers stay here."""
        payload = self._json("POST", path, json_body=dict(body))
        if not isinstance(payload, dict):
            raise AirwallexError(200, "unexpected_body", "expected a JSON object")
        return payload

    def _json(self, method: str, path: str, *, query=None, json_body=None):
        response = self._send(method, path, query=query, json_body=json_body)
        if not response.body:
            return None
        return json.loads(response.body.decode(), parse_float=Decimal)

    def _send(
        self,
        method: str,
        path: str,
        *,
        query=None,
        json_body=None,
        auth: bool = True,
        accept: str = "application/json",
    ) -> HttpResponse:
        with self._lock:
            refreshed = False
            rate_limited = 0
            while True:
                if auth:
                    self._ensure_token()
                self._acquire()
                response = self._transport.send(
                    self._build(method, path, query=query, json_body=json_body, auth=auth, accept=accept)
                )
                if auth and response.status == 401 and not refreshed:
                    self._token = None
                    self._expires_at = 0.0
                    refreshed = True
                    continue
                if response.status == 429 and rate_limited < MAX_RATE_LIMIT_RETRIES:
                    self._sleep(self._retry_delay(response, rate_limited))
                    rate_limited += 1
                    continue
                if response.status >= 400:
                    raise self._error(response)
                return response

    def _ensure_token(self) -> None:
        if self._token is not None and self._clock() < self._expires_at - REFRESH_MARGIN_SECONDS:
            return
        response = self._send("POST", "/api/v1/authentication/login", json_body={}, auth=False)
        payload = json.loads(response.body.decode() or "null")
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise AirwallexError(response.status, "login_failed", "login response had no token")
        self._token = token
        self._expires_at = _parse_expiry(payload.get("expires_at") if isinstance(payload, dict) else None, self._clock())

    def _build(self, method, path, *, query, json_body, auth: bool, accept: str) -> HttpRequest:
        headers = {"Accept": accept, "x-api-version": API_VERSION}
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body, default=_json_default, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        else:
            headers["x-client-id"] = self._client_id
            headers["x-api-key"] = self._api_key
        return HttpRequest(method, self._url(path, query), headers, body)

    def _url(self, path: str, query) -> str:
        url = self._base_url + path
        if not query:
            return url
        pairs = [(key, value) for key, value in query if value is not None]
        if not pairs:
            return url
        return url + "?" + urllib.parse.urlencode(pairs)

    def _acquire(self) -> None:
        now_ms = int(self._clock() * 1000)
        if self._next_ms > now_ms:
            self._sleep((self._next_ms - now_ms) / 1000)
            now_ms = self._next_ms
        self._next_ms = now_ms + int(MIN_INTERVAL_SECONDS * 1000)

    def _retry_delay(self, response: HttpResponse, attempt: int) -> float:
        for key, value in response.headers.items():
            if key.lower() == "retry-after":
                try:
                    return max(0.0, float(value))
                except ValueError:
                    break
        return 0.5 * (2**attempt)

    def _error(self, response: HttpResponse) -> AirwallexError:
        code = None
        message = ""
        field_codes: tuple[str, ...] = ()
        try:
            payload = json.loads(response.body.decode() or "null")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            code = raw_code if isinstance(raw_code, str) else None
            raw_message = payload.get("message")
            message = raw_message if isinstance(raw_message, str) else ""
            field_codes = _field_codes(payload.get("details"))
        return AirwallexError(response.status, code, message, field_codes)


def _json_default(value):
    if isinstance(value, Decimal):
        integral = value.to_integral_value()
        if value == integral:
            return int(integral)
        return float(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def _parse_expiry(value, now: float) -> float:
    if not isinstance(value, str) or not value:
        return now + 25 * 60
    text = value.replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and ":" not in text[-5:]:
        text = text[:-2] + ":" + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return now + 25 * 60
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _path_id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _field_codes(details) -> tuple[str, ...]:
    if not isinstance(details, dict):
        return ()
    errors = details.get("errors")
    if not isinstance(errors, list):
        return ()
    return tuple(str(item["code"]) for item in errors if isinstance(item, dict) and item.get("code") is not None)


def _optional_str(value) -> str | None:
    if value is None:
        return None
    return str(value)

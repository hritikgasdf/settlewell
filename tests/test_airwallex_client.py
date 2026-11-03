"""Airwallex adapter: cached login, pinned version, throttle, typed calls."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.airwallex import (
    AirwallexClient,
    AirwallexError,
    MissingCredentials,
)
from backend.airwallex.http import HttpRequest, HttpResponse


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[HttpRequest] = []
        self._queue: list[HttpResponse] = []

    def push(self, status: int, body, headers: dict[str, str] | None = None) -> None:
        if isinstance(body, (dict, list)) or body is None or isinstance(body, str):
            raw = json.dumps(body).encode()
            default_headers = {"Content-Type": "application/json"}
        else:
            raw = body
            default_headers = {"Content-Type": "application/pdf"}
        self._queue.append(HttpResponse(status, headers or default_headers, raw))

    def send(self, request: HttpRequest) -> HttpResponse:
        self.calls.append(request)
        if not self._queue:
            raise AssertionError(f"unexpected {request.method} {request.url}")
        return self._queue.pop(0)


def client(transport: FakeTransport, clock: FakeClock) -> AirwallexClient:
    return AirwallexClient(
        client_id="client-id",
        api_key="super-secret-key",
        base_url="https://api-demo.airwallex.com",
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
    )


def login(clock: FakeClock, *, expires_in: int = 1800, token: str = "cached-token") -> dict:
    expires = datetime.fromtimestamp(clock.now + expires_in, UTC)
    return {"token": token, "expires_at": expires.isoformat()}


def test_from_env_reports_missing_names_only():
    with pytest.raises(MissingCredentials) as caught:
        AirwallexClient.from_env(env={})
    assert caught.value.missing == (
        "AIRWALLEX_SANDBOX_CLIENT_ID",
        "AIRWALLEX_SANDBOX_API_KEY",
    )
    assert "super-secret-key" not in str(caught.value)


def test_login_is_cached_and_version_is_pinned():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock))
    transport.push(200, [{"currency": "EUR", "available_amount": 10, "pending_amount": 0, "reserved_amount": 0, "total_amount": 10, "prepayment_amount": 0, "account_type": "AVAILABLE"}])
    transport.push(200, [{"currency": "EUR", "available_amount": 10, "pending_amount": 0, "reserved_amount": 0, "total_amount": 10, "prepayment_amount": 0, "account_type": "AVAILABLE"}])
    api = client(transport, clock)

    first = api.current_balances()
    second = api.current_balances()

    assert first[0].currency == "EUR"
    assert first[0].available_amount == Decimal(10)
    assert second[0].currency == "EUR"
    logins = [call for call in transport.calls if call.url.endswith("/api/v1/authentication/login")]
    assert len(logins) == 1
    assert logins[0].method == "POST"
    assert json.loads(logins[0].body) == {}
    assert logins[0].headers["x-client-id"] == "client-id"
    assert logins[0].headers["x-api-key"] == "super-secret-key"
    assert logins[0].headers["x-api-version"] == "2025-11-11"
    assert "Authorization" not in logins[0].headers
    for call in transport.calls[1:]:
        assert call.headers["x-api-version"] == "2025-11-11"
        assert call.headers["Authorization"] == "Bearer cached-token"
        assert "x-api-key" not in call.headers
        assert "x-client-id" not in call.headers


def test_token_inside_refresh_window_logs_in_again():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock, expires_in=30, token="soon"))
    transport.push(200, [{"currency": "USD", "available_amount": 1, "pending_amount": 0, "reserved_amount": 0, "total_amount": 1, "prepayment_amount": 0, "account_type": "AVAILABLE"}])
    transport.push(201, login(clock, expires_in=1800, token="fresh"))
    transport.push(200, [{"currency": "USD", "available_amount": 2, "pending_amount": 0, "reserved_amount": 0, "total_amount": 2, "prepayment_amount": 0, "account_type": "AVAILABLE"}])
    api = client(transport, clock)

    assert api.current_balances()[0].available_amount == Decimal(1)
    assert api.current_balances()[0].available_amount == Decimal(2)
    tokens = [call.headers.get("Authorization") for call in transport.calls if "Authorization" in call.headers]
    assert tokens == ["Bearer soon", "Bearer fresh"]


def test_401_relogs_in_once_then_stops():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock, token="old"))
    transport.push(401, {"code": "unauthorized", "message": "expired"})
    transport.push(201, login(clock, token="new"))
    transport.push(401, {"code": "unauthorized", "message": "expired"})
    api = client(transport, clock)

    with pytest.raises(AirwallexError) as caught:
        api.current_balances()
    assert caught.value.status_code == 401
    assert caught.value.code == "unauthorized"
    assert "super-secret-key" not in str(caught.value)
    logins = [call for call in transport.calls if call.url.endswith("/authentication/login")]
    assert len(logins) == 2


def test_requests_stay_at_or_under_five_per_second():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock))
    for _ in range(3):
        transport.push(200, [])
    api = client(transport, clock)

    api.current_balances()
    api.current_balances()
    api.current_balances()

    assert clock.slept == [0.2, 0.2, 0.2]


def test_429_honors_retry_after_then_returns():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock))
    transport.push(429, {"code": "too_many_requests", "message": "slow down"}, {"Content-Type": "application/json", "Retry-After": "2"})
    transport.push(200, [])
    api = client(transport, clock)

    assert api.current_balances() == []
    assert 2.0 in clock.slept


def test_429_without_retry_after_backs_off_then_raises():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock))
    for _ in range(4):
        transport.push(429, {"code": "too_many_requests", "message": "slow down"})
    api = client(transport, clock)

    with pytest.raises(AirwallexError) as caught:
        api.current_balances()
    assert caught.value.code == "too_many_requests"
    assert 0.5 in clock.slept
    assert 1.0 in clock.slept
    assert 2.0 in clock.slept


def test_typed_calls_for_balances_transfers_beneficiaries_letters_and_fx():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock))
    history = {
        "has_more": False,
        "items": [{
            "id": "hist-1",
            "source": "tr_1",
            "amount": -13.47,
            "currency": "EUR",
            "balance": 100,
            "description": "Fee",
            "fee": 0,
            "posted_at": "2026-11-03T00:00:00+0000",
            "source_type": "FEE",
            "transaction_type": "FEE",
            "account_type": "AVAILABLE",
            "reference": "INV-2291",
        }],
    }
    transfer = {
        "id": "tr_1",
        "request_id": "stl-inv2291-1",
        "status": "SENT",
        "transfer_amount": 4200,
        "transfer_currency": "EUR",
        "source_amount": 4200,
        "source_currency": "EUR",
        "fee_amount": 13.47,
        "fee_currency": "EUR",
        "short_reference_id": "P261103-TEST",
        "reference": "INV-2291",
        "beneficiary_id": "ben_1",
        "transfer_date": "2026-11-03",
        "dispatch_date": "2026-11-03",
        "transfer_method": "SWIFT",
        "failure_reason": "90701: Account closed",
        "failure_type": "INCORRECT_ROUTING",
    }
    transport.push(200, history)
    transport.push(200, {"items": [transfer], "page_after": None})
    transport.push(200, transfer)
    transport.push(201, transfer)
    transport.push(200, "OK")
    transport.push(200, "OK")
    transport.push(201, {"id": "ben_2", "nickname": "Lumen new"})
    transport.push(200, b"%PDF-1.4", {"Content-Type": "application/pdf"})
    transport.push(201, {
        "quote_id": "quo_1",
        "client_rate": 1.13,
        "buy_currency": "EUR",
        "sell_currency": "USD",
        "buy_amount": 100,
        "sell_amount": 88.5,
        "valid_to_at": "2026-11-03T12:01:00+0000",
        "usage": "MULTI_USE",
    })
    transport.push(200, {
        "quote_id": "quo_1",
        "client_rate": 1.13,
        "buy_currency": "EUR",
        "sell_currency": "USD",
        "valid_to_at": "2026-11-03T12:01:00+0000",
        "usage": "MULTI_USE",
    })
    transport.push(201, {"conversion_id": "con_1", "status": "SETTLED", "quote_id": "quo_1"})
    api = client(transport, clock)

    page = api.balance_history(currency="EUR")
    listed = api.list_transfers(request_id="stl-inv2291-1")
    loaded = api.get_transfer("tr_1")
    created = api.create_transfer(
        transfer_amount=Decimal(4200),
        transfer_currency="EUR",
        source_currency="EUR",
        beneficiary_id="ben_1",
        transfer_method="SWIFT",
        reference="INV-2291",
        reason="goods",
        request_id="stl-inv2291-1",
        transfer_date="2026-11-03",
        metadata={"obligation_id": "INV-2291"},
    )
    cancelled = api.cancel_transfer("tr_1")
    validation = api.validate_beneficiary({"beneficiary": {"entity_type": "COMPANY"}})
    beneficiary = api.create_beneficiary({"nickname": "Lumen new"})
    letter = api.create_confirmation_letter(transaction_id="tr_1", letter_format="NO_FEE_DISPLAY")
    quote = api.create_quote(
        buy_currency="EUR",
        sell_currency="USD",
        validity="MIN_1",
        buy_amount=Decimal(100),
    )
    fetched = api.get_quote("quo_1")
    conversion = api.create_conversion(quote_id="quo_1", buy_currency="EUR", sell_currency="USD", buy_amount=Decimal(100))

    assert page.items[0].source == "tr_1"
    assert page.items[0].transaction_type == "FEE"
    assert page.items[0].amount == Decimal("-13.47")
    assert listed.items[0].short_reference_id == "P261103-TEST"
    assert loaded.failure_reason == "90701: Account closed"
    assert loaded.dispatch_date == "2026-11-03"
    assert created.request_id == "stl-inv2291-1"
    assert cancelled.ok is True
    assert validation.ok is True
    assert beneficiary.id == "ben_2"
    assert letter.content.startswith(b"%PDF")
    assert letter.media_type == "application/pdf"
    assert quote.quote_id == "quo_1"
    assert quote.usage == "MULTI_USE"
    assert quote.valid_to_at == "2026-11-03T12:01:00+0000"
    assert fetched.client_rate == Decimal("1.13")
    assert conversion.conversion_id == "con_1"
    assert conversion.status == "SETTLED"

    paths = [call.url.removeprefix("https://api-demo.airwallex.com") for call in transport.calls]
    assert paths == [
        "/api/v1/authentication/login",
        "/api/v1/balances/history?currency=EUR&page_size=10",
        "/api/v1/transfers?request_id=stl-inv2291-1",
        "/api/v1/transfers/tr_1",
        "/api/v1/transfers/create",
        "/api/v1/transfers/tr_1/cancel",
        "/api/v1/beneficiaries/validate",
        "/api/v1/beneficiaries/create",
        "/api/v1/confirmation_letters/create",
        "/api/v1/fx/quotes/create",
        "/api/v1/fx/quotes/quo_1",
        "/api/v1/fx/conversions/create",
    ]
    create_body = json.loads(transport.calls[4].body)
    assert create_body["transfer_amount"] == 4200
    assert create_body["metadata"] == {"obligation_id": "INV-2291"}
    assert "headers" not in create_body
    letter_body = json.loads(transport.calls[8].body)
    assert letter_body == {"format": "NO_FEE_DISPLAY", "transaction_id": "tr_1"}
    assert transport.calls[8].headers["Accept"] == "*/*"
    for call in transport.calls:
        assert call.headers["x-api-version"] == "2025-11-11"


def test_cancel_object_is_parsed_as_a_transfer():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock))
    transport.push(200, {"id": "tr_9", "status": "CANCELLED", "request_id": "stl-x-1", "transfer_currency": "EUR", "transfer_amount": 1})
    api = client(transport, clock)

    result = api.cancel_transfer("tr_9")

    assert result.ok is True
    assert result.transfer is not None
    assert result.transfer.status == "CANCELLED"
    assert result.transfer.id == "tr_9"


def test_validate_reports_iban_code_083_instead_of_raising():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock))
    transport.push(
        400,
        {
            "code": "validation_failed",
            "message": "This is not valid.",
            "details": {
                "errors": [{"source": "beneficiary.bank_details.iban", "code": "083"}],
            },
        },
    )
    api = client(transport, clock)

    result = api.validate_beneficiary({"beneficiary": {"bank_details": {"iban": "not-an-iban"}}})

    assert result.ok is False
    assert result.field_codes == ("083",)


def test_validate_still_raises_when_the_service_is_down():
    transport = FakeTransport()
    clock = FakeClock()
    transport.push(201, login(clock))
    transport.push(400, {"code": "service_unavailable", "message": "try later"})
    api = client(transport, clock)

    with pytest.raises(AirwallexError) as caught:
        api.validate_beneficiary({"beneficiary": {}})
    assert caught.value.code == "service_unavailable"

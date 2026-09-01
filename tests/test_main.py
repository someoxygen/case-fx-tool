from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from main import create_app

VALID_PARAMS = {
    "amount": "250",
    "from": "EUR",
    "to": "TRY",
    "date": "2024-08-28",
}


@pytest.fixture(autouse=True)
def closed_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every app still points at a closed port; MockTransport is the only HTTP seam.
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://127.0.0.1:1/")


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
    transport = httpx.MockTransport(handler)
    return TestClient(create_app(upstream_transport=transport))


def quote_response(
    request: httpx.Request,
    *,
    rate: float = 47.1234,
    rate_date: str = "2024-08-28",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={"base": "EUR", "date": rate_date, "rates": {"TRY": rate}},
        request=request,
    )


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert set(response.json()) == {"error", "message"}
    assert response.json()["error"] == code
    assert isinstance(response.json()["message"], str)
    assert response.json()["message"]


def test_successful_historical_conversion_preserves_rate_precision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/2024-08-28"
        assert dict(request.url.params) == {"base": "EUR", "symbols": "TRY"}
        return quote_response(request)

    with client_for(handler) as client:
        response = client.get("/tools/convert", params=VALID_PARAMS)

    assert response.status_code == 200
    assert response.json() == {
        "amount": 250,
        "from": "EUR",
        "to": "TRY",
        "rate": 47.1234,
        "result": 11780.85,
        "rate_date": "2024-08-28",
        "asked_date": "2024-08-28",
        "source": "ECB via frankfurter.dev",
    }


def test_public_query_names_are_exact_and_currencies_are_normalized() -> None:
    application = create_app(upstream_transport=httpx.MockTransport(quote_response))
    parameters = application.openapi()["paths"]["/tools/convert"]["get"]["parameters"]
    assert [parameter["name"] for parameter in parameters] == [
        "amount",
        "from",
        "to",
        "date",
    ]
    assert all(parameter["required"] for parameter in parameters)

    params = {**VALID_PARAMS, "from": "eur", "to": "try"}
    with TestClient(application) as client:
        response = client.get("/tools/convert", params=params)
    assert response.status_code == 200
    assert response.json()["from"] == "EUR"
    assert response.json()["to"] == "TRY"


def test_weekend_carry_forward_keeps_asked_and_rate_dates_distinct() -> None:
    params = {**VALID_PARAMS, "date": "2024-08-31"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/2024-08-31"
        return quote_response(request, rate_date="2024-08-30")

    with client_for(handler) as client:
        response = client.get("/tools/convert", params=params)

    assert response.status_code == 200
    assert response.json()["asked_date"] == "2024-08-31"
    assert response.json()["rate_date"] == "2024-08-30"


def test_future_date_is_rejected_without_calling_upstream() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("future requests must not reach the upstream")

    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    with client_for(handler) as client:
        response = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "date": tomorrow},
        )

    assert_error(response, 422, "future_date")
    assert calls == 0


def test_date_before_ecb_series_is_rejected_without_calling_upstream() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("pre-series requests must not reach the upstream")

    with client_for(handler) as client:
        response = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "date": "1999-01-03"},
        )

    assert_error(response, 422, "date_before_series")
    assert calls == 0


def test_same_currency_is_rejected() -> None:
    with client_for(quote_response) as client:
        response = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "to": "eur"},
        )
    assert_error(response, 422, "same_currency")


@pytest.mark.parametrize("currency", ["EU", "EURO", "E1R", "\u20acUR", ""])
def test_malformed_currency_is_rejected(currency: str) -> None:
    with client_for(quote_response) as client:
        response = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "from": currency},
        )
    assert_error(response, 422, "invalid_currency")


def test_well_formed_but_unavailable_pair_is_non_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"}, request=request)

    with client_for(handler) as client:
        response = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "to": "ZZZ"},
        )
    assert_error(response, 422, "rate_unavailable")


def test_missing_amount_uses_documented_error_shape() -> None:
    params = {key: value for key, value in VALID_PARAMS.items() if key != "amount"}
    with client_for(quote_response) as client:
        response = client.get("/tools/convert", params=params)
    assert_error(response, 422, "invalid_request")


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity"])
def test_non_positive_or_non_finite_amount_is_rejected(amount: str) -> None:
    with client_for(quote_response) as client:
        response = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "amount": amount},
        )
    assert_error(response, 422, "invalid_amount")


def test_amount_with_excess_fractional_precision_is_rejected() -> None:
    with client_for(quote_response) as client:
        response = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "amount": "1.1234567890"},
        )
    assert_error(response, 422, "invalid_amount")


def test_compact_or_invalid_date_is_rejected() -> None:
    with client_for(quote_response) as client:
        compact = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "date": "20240828"},
        )
        invalid = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "date": "2024-02-30"},
        )
    assert_error(compact, 422, "invalid_request")
    assert_error(invalid, 422, "invalid_request")


def test_upstream_timeout_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with client_for(handler) as client:
        response = client.get("/tools/convert", params=VALID_PARAMS)
    assert_error(response, 504, "upstream_timeout")


def test_upstream_connection_failure_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with client_for(handler) as client:
        response = client.get("/tools/convert", params=VALID_PARAMS)
    assert_error(response, 502, "upstream_error")


def test_upstream_500_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="failure details", request=request)

    with client_for(handler) as client:
        response = client.get("/tools/convert", params=VALID_PARAMS)
    assert_error(response, 502, "upstream_error")
    assert "failure details" not in response.text


def test_upstream_non_json_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>failure</html>", request=request)

    with client_for(handler) as client:
        response = client.get("/tools/convert", params=VALID_PARAMS)
    assert_error(response, 502, "upstream_invalid_response")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"base": "EUR", "date": "2024-08-28"},
        {"base": "USD", "date": "2024-08-28", "rates": {"TRY": 47.1}},
        {"base": "EUR", "date": "not-a-date", "rates": {"TRY": 47.1}},
        {"base": "EUR", "date": "2024-08-29", "rates": {"TRY": 47.1}},
        {"base": "EUR", "date": "2024-08-28", "rates": {"TRY": 0}},
    ],
)
def test_malformed_upstream_schema_is_reported(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    with client_for(handler) as client:
        response = client.get("/tools/convert", params=VALID_PARAMS)
    assert_error(response, 502, "upstream_invalid_response")


def test_missing_target_rate_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"base": "EUR", "date": "2024-08-28", "rates": {}},
            request=request,
        )

    with client_for(handler) as client:
        response = client.get("/tools/convert", params=VALID_PARAMS)
    assert_error(response, 422, "rate_unavailable")


def test_repeated_pair_and_date_uses_cached_validated_rate() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return quote_response(request)

    with client_for(handler) as client:
        first = client.get("/tools/convert", params=VALID_PARAMS)
        second = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "amount": "10"},
        )

    assert first.status_code == second.status_code == 200
    assert second.json()["result"] == 471.23
    assert calls == 1


def test_different_date_for_same_pair_is_not_served_from_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked_date = request.url.path.rsplit("/", 1)[-1]
        calls.append(asked_date)
        rate = 47.0 if asked_date == "2024-08-28" else 48.0
        return quote_response(request, rate=rate, rate_date=asked_date)

    with client_for(handler) as client:
        first = client.get("/tools/convert", params=VALID_PARAMS)
        second = client.get(
            "/tools/convert",
            params={**VALID_PARAMS, "date": "2024-08-29"},
        )

    assert first.json()["rate"] == 47.0
    assert second.json()["rate"] == 48.0
    assert calls == ["2024-08-28", "2024-08-29"]


def test_failed_response_is_not_cached() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, request=request)
        return quote_response(request)

    with client_for(handler) as client:
        first = client.get("/tools/convert", params=VALID_PARAMS)
        second = client.get("/tools/convert", params=VALID_PARAMS)

    assert_error(first, 502, "upstream_error")
    assert second.status_code == 200
    assert calls == 2

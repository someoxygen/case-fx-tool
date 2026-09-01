"""Small, safe HTTP wrapper around Frankfurter's ECB v1 rates."""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Annotated

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

DEFAULT_UPSTREAM_BASE = "https://api.frankfurter.dev"
ECB_SERIES_START = date(1999, 1, 4)
UPSTREAM_TIMEOUT_SECONDS = 5.0
MONEY_QUANTUM = Decimal("0.01")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateQuote:
    rate: Decimal
    rate_date: date


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "message": message},
    )


def _parse_amount(raw_amount: str) -> Decimal:
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation as exc:
        raise ServiceError(422, "invalid_amount", "Amount must be a decimal number.") from exc

    if not amount.is_finite() or amount <= 0:
        raise ServiceError(422, "invalid_amount", "Amount must be greater than zero.")

    fractional_digits = max(0, -amount.as_tuple().exponent)
    if fractional_digits > 2:
        raise ServiceError(
            422,
            "invalid_amount",
            "Amount may have at most two fractional digits.",
        )
    return amount


def _parse_currency(raw_currency: str) -> str:
    currency = raw_currency.upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise ServiceError(
            422,
            "invalid_currency",
            "Currency codes must contain exactly three ASCII letters.",
        )
    return currency


def _parse_date(raw_date: str) -> date:
    if not DATE_PATTERN.fullmatch(raw_date):
        raise ServiceError(422, "invalid_request", "Date must use YYYY-MM-DD format.")
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ServiceError(422, "invalid_request", "Date must be a valid calendar date.") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _decode_quote(payload_text: str, source: str, target: str, asked_date: date) -> RateQuote:
    try:
        payload = json.loads(
            payload_text,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ServiceError(
            502,
            "upstream_invalid_response",
            "The FX provider returned invalid JSON.",
        ) from exc

    if not isinstance(payload, dict) or payload.get("base") != source:
        raise ServiceError(
            502,
            "upstream_invalid_response",
            "The FX provider returned an unexpected response.",
        )

    rates = payload.get("rates")
    if not isinstance(rates, dict):
        raise ServiceError(
            502,
            "upstream_invalid_response",
            "The FX provider returned an unexpected response.",
        )
    if target not in rates:
        raise ServiceError(
            422,
            "rate_unavailable",
            "No rate is available for the requested currency pair and date.",
        )

    rate = rates[target]
    if not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0:
        raise ServiceError(
            502,
            "upstream_invalid_response",
            "The FX provider returned an invalid exchange rate.",
        )

    raw_rate_date = payload.get("date")
    if not isinstance(raw_rate_date, str) or not DATE_PATTERN.fullmatch(raw_rate_date):
        raise ServiceError(
            502,
            "upstream_invalid_response",
            "The FX provider returned an invalid rate date.",
        )
    try:
        rate_date = date.fromisoformat(raw_rate_date)
    except ValueError as exc:
        raise ServiceError(
            502,
            "upstream_invalid_response",
            "The FX provider returned an invalid rate date.",
        ) from exc

    if rate_date < ECB_SERIES_START or rate_date > asked_date:
        raise ServiceError(
            502,
            "upstream_invalid_response",
            "The FX provider returned an impossible rate date.",
        )
    return RateQuote(rate=rate, rate_date=rate_date)


async def _fetch_rate(
    client: httpx.AsyncClient,
    upstream_base: str,
    cache: dict[tuple[str, str, date], RateQuote],
    source: str,
    target: str,
    asked_date: date,
) -> RateQuote:
    key = (source, target, asked_date)
    if key in cache:
        return cache[key]

    url = f"{upstream_base}/v1/{asked_date.isoformat()}"
    try:
        response = await client.get(url, params={"base": source, "symbols": target})
    except httpx.TimeoutException as exc:
        raise ServiceError(504, "upstream_timeout", "The FX provider timed out.") from exc
    except httpx.RequestError as exc:
        raise ServiceError(502, "upstream_error", "The FX provider could not be reached.") from exc

    if 400 <= response.status_code < 500:
        raise ServiceError(
            422,
            "rate_unavailable",
            "No rate is available for the requested currency pair and date.",
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise ServiceError(502, "upstream_error", "The FX provider returned an error.")

    quote = _decode_quote(response.text, source, target, asked_date)
    cache[key] = quote
    return quote


def create_app(
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    upstream_base: str | None = None,
) -> FastAPI:
    """Create the service; injectable transport keeps tests off the network."""

    configured_base = (
        upstream_base
        if upstream_base is not None
        else os.getenv("FX_UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE)
    ).rstrip("/")
    rate_cache: dict[tuple[str, str, date], RateQuote] = {}

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS),
            transport=upstream_transport,
        ) as client:
            application.state.upstream_client = client
            yield

    application = FastAPI(title="fx-tool", version="1.0", lifespan=lifespan)

    @application.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            "invalid_request",
            "Required query parameters are missing or malformed.",
        )

    @application.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unexpected conversion failure", exc_info=exc)
        return _error_response(500, "internal_error", "An unexpected internal error occurred.")

    @application.get("/tools/convert", response_model=None)
    async def convert(
        request: Request,
        amount: Annotated[str, Query(max_length=100)],
        from_: Annotated[str, Query(alias="from")],
        to: Annotated[str, Query()],
        asked_date_text: Annotated[str, Query(alias="date", max_length=10)],
    ) -> dict[str, Decimal | str]:
        parsed_amount = _parse_amount(amount)
        source = _parse_currency(from_)
        target = _parse_currency(to)
        asked_date = _parse_date(asked_date_text)

        if source == target:
            raise ServiceError(
                422,
                "same_currency",
                "Source and target currencies must be different.",
            )

        today_utc = datetime.now(timezone.utc).date()
        if asked_date > today_utc:
            raise ServiceError(422, "future_date", "Date must not be in the future.")
        if asked_date < ECB_SERIES_START:
            raise ServiceError(
                422,
                "date_before_series",
                "Date must be on or after 1999-01-04.",
            )

        quote = await _fetch_rate(
            request.app.state.upstream_client,
            configured_base,
            rate_cache,
            source,
            target,
            asked_date,
        )
        with localcontext() as context:
            context.prec = max(
                28,
                len(parsed_amount.as_tuple().digits) + len(quote.rate.as_tuple().digits) + 4,
            )
            result = (parsed_amount * quote.rate).quantize(
                MONEY_QUANTUM,
                rounding=ROUND_HALF_UP,
            )

        return {
            "amount": parsed_amount,
            "from": source,
            "to": target,
            "rate": quote.rate,
            "result": result,
            "rate_date": quote.rate_date.isoformat(),
            "asked_date": asked_date.isoformat(),
            "source": "ECB via frankfurter.dev",
        }

    return application


app = create_app()

# FX conversion tool

## What it does

This FastAPI service exposes one agent-friendly endpoint that converts a positive fiat amount using a validated historical ECB rate from Frankfurter v1. It keeps the caller's requested date separate from the actual rate date, never substitutes a made-up rate, and caches validated rates in the running process.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
./run.sh
```

`PORT` controls the listening port (default `8080`). `FX_UPSTREAM_BASE` controls the upstream origin (default `https://api.frankfurter.dev`); a trailing slash is accepted. `run.sh` binds to `0.0.0.0` and leaves upstream configuration to the application.

## Example

```bash
curl 'http://localhost:8080/tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28'
```

```json
{
  "amount": 250,
  "from": "EUR",
  "to": "TRY",
  "rate": 47.1234,
  "result": 11780.85,
  "rate_date": "2026-08-28",
  "asked_date": "2026-08-28",
  "source": "ECB via frankfurter.dev"
}
```

The shown rate is illustrative; the live value comes from the configured upstream.

## Tests

```bash
./test.sh
```

Tests replace the HTTP transport with a fake upstream and require no network. They remain isolated even when `FX_UPSTREAM_BASE` points to a closed port.

## Behavior / edge cases

- `date` is required and must be a valid `YYYY-MM-DD` UTC calendar date. Future dates and dates before the ECB series start (`1999-01-04`) are rejected before any upstream call.
- The service makes one v1 historical request for the exact date asked. Frankfurter may carry a weekend or holiday back to the prior ECB observation: `asked_date` remains the caller's date and validated upstream `date` becomes `rate_date`. There is no `/latest` fallback.
- Currency codes are normalized to uppercase and must be exactly three ASCII letters. Equal currencies are rejected. A well-formed but unsupported code or pair returns `rate_unavailable` when the upstream cannot supply it; there is no per-request currencies preflight.
- Amounts must be finite, greater than zero, and have at most two fractional digits. Input is never silently rounded. Calculation uses `Decimal`, preserves the upstream rate precision, then rounds only the result to two places with `ROUND_HALF_UP`.
- Timeouts return 504. Transport failures, upstream 5xx responses, non-JSON bodies, invalid schemas, non-positive rates, and impossible rate dates return 502; upstream details are not exposed.
- Only validated `(from, to, asked_date)` rate/date pairs are cached in memory. The amount is intentionally not part of the key. Failures are not cached, and another date always causes another upstream request.

## Error codes

| Code | HTTP status | Meaning |
|---|---:|---|
| `invalid_request` | 422 | A required query value is missing, malformed, or the date is invalid. |
| `invalid_amount` | 422 | Amount is non-numeric, non-finite, non-positive, or too precise. |
| `invalid_currency` | 422 | A currency is not exactly three ASCII letters. |
| `same_currency` | 422 | Source and target currencies are equal. |
| `future_date` | 422 | The requested date is after today's UTC date. |
| `date_before_series` | 422 | The requested date predates `1999-01-04`. |
| `rate_unavailable` | 422 | The upstream has no requested pair/date rate or returns a 4xx response. |
| `upstream_timeout` | 504 | The upstream request exceeded the five-second timeout. |
| `upstream_error` | 502 | The upstream was unreachable or returned a non-2xx/4xx failure. |
| `upstream_invalid_response` | 502 | The upstream body, schema, rate, or rate date was invalid. |
| `internal_error` | 500 | An unexpected service error occurred. |

# Review of `tool.py`

Findings are ranked by likely customer harm.

## 1. Documented requests silently use different inputs

The route exposes `from_` and `on`, not the required public query names `from` and `date`; neither parameter has a FastAPI alias. Both also have defaults. A customer or agent can send the documented request and receive a plausible 200 conversion made with EUR and `/latest` instead of the USD and historical date it asked for. The response also omits `asked_date`, removing the caller's best way to detect the substitution. I verified the OpenAPI parameter list is `amount, from_, to, on`; a fake-upstream request using `from=USD&date=2024-08-28` called `/latest?base=EUR` and returned today's date.

The hardcoded `UPSTREAM` compounds the contract failure: `FX_UPSTREAM_BASE` is ignored, so a configured reviewer or deployment cannot redirect traffic. I would verify this by setting the variable to a fake origin and asserting that the fake receives the call; the current code still targets the public host.

## 2. Failures become successful zero-valued financial answers

The broad `except Exception` turns timeouts, connection failures, upstream errors, non-JSON, missing keys, unsupported currencies, and programmer defects into HTTP 200 with `rate: 0.0` and `result: 0.0`. An AI agent cannot distinguish an outage from a real conversion and may tell a paying customer that their money is worth zero. There is also no status check or payload validation before indexing JSON. I reproduced this with a fake response whose `json()` raised `ValueError`: the endpoint returned 200 and zeros. `httpx` does have a default timeout; the defect is swallowing that and every other failure.

## 3. Cache and date handling corrupt rate provenance

The cache key contains only the currency pair, so a second request for another date reuses the first rate. Worse, `rate_date` is fabricated from `on` or local `date.today()` instead of the upstream payload's `date`. The `/latest` fallback can also turn weekends, future/pre-series dates, or unsupported pairs into an unrelated current value. Customers receive a believable number labeled as belonging to a day it may not represent. With a fake upstream, I requested the same pair for two dates: only one upstream call occurred, but the same cached rate was labeled once with each requested date.

## 4. Rounding the rate before multiplication changes the answer

`round(rate, 2)` discards upstream precision before calculation. For the sample rate, `47.1234` becomes `47.12`, so 250 EUR produces `11780.00` instead of `11780.85`. I reproduced this with a fake `47.1234` response and compared the endpoint output with a Decimal calculation using the full rate.

## The one I would fix before shipping tonight

I would fix the public API contract first. A request that follows the published interface currently succeeds while silently ignoring its source currency and historical date, so even healthy upstream behavior produces the wrong answer. Making `from` and `date` required aliases and returning `asked_date` restores the minimum trustworthy agent contract; the other correctness fixes must follow before broader release.

## Things that look suspicious but are fine

An in-process cache is appropriate for this assignment; Redis or a database would add no value. Omitting `amount` from the rate-cache key is also correct because amount does not change the rate; the missing requested date is the bug. The extra `/health` route is unnecessary but not customer-harmful, and it would be inaccurate to claim `httpx.AsyncClient` has literally no timeout because `httpx` supplies a default.

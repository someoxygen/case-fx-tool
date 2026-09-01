# Notes

## Decisions

I used FastAPI and one shared `httpx.AsyncClient` because the endpoint is asynchronous and the HTTP boundary stays easy to fake. The app deliberately calls Frankfurter v1, not v2: v1 is the stable ECB-only contract in the case and returns the aggregate `rates` shape expected by the review fake.

`date` is required. Each cache miss makes one request to `/v1/{asked_date}`; there is no speculative currencies request and no `/latest` fallback. Frankfurter v1 carries non-business dates back to the last ECB observation, so the caller's date is returned as `asked_date` while the validated upstream `date` is authoritative for `rate_date`. Dates after today's UTC date and before the documented ECB start (`1999-01-04`) are rejected locally.

Amounts and rates use `Decimal`. Input allows at most two fractional digits and is not silently changed. The full upstream rate is used in multiplication; only the final monetary result is quantized to two decimal places with `ROUND_HALF_UP`. Upstream 4xx/missing-pair outcomes map to `rate_unavailable`; timeouts, transport/5xx failures, and invalid payloads remain distinct non-2xx errors.

The cache is intentionally process-local and stores only validated `(rate, rate_date)` values under `(source, target, asked_date)`. Amount is not part of a rate key, and failures never enter the cache.

## With another day

I would bound the cache and add a TTL, coalesce concurrent misses for the same key, and add structured logs and metrics around latency, upstream failures, cache hits, and rate-date carry-forward. I would also add cancellation-aware, tightly bounded retries for safe transient GET failures, more exhaustive upstream contract fixtures, and deployment hardening. None of these is implemented here.

## AI tools

I used Codex in VS Code as an implementation and review assistant: to inspect the brief, draft the small service and tests, enumerate edge cases, and iterate on test failures. I verified the final behavior against the repository contract, Frankfurter's v1 documentation, deterministic endpoint tests, and a manual review of the diff rather than treating generated code as authoritative.

## One thing the AI got wrong

The first draft imported `localcontext` from `contextlib`; it actually belongs to `decimal`. The initial test collection failed immediately with that import error. I corrected the import and reran the complete suite. A later endpoint assertion also caught Decimal fields being serialized as strings by inferred response-model handling, so I disabled that inference for the deliberately shaped numeric response and verified the JSON types.

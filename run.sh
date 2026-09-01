#!/usr/bin/env bash
# Starts the service. It must listen on $PORT (default 8080) and read the
# upstream base URL from $FX_UPSTREAM_BASE — we point that at a fake upstream
# when we review your work, so nothing here may hardcode frankfurter.dev.
set -euo pipefail

port="${PORT:-8080}"
exec uvicorn main:app --host 0.0.0.0 --port "$port"

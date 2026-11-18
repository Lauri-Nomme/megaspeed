#!/bin/bash
# usage: run_fingerprint.sh <label> [tenants...]
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$BENCH")"
PY="$REPO/.venv/bin/python"
LABEL="$1"; shift
PORT=8797
for tenant in "$@"; do
  cd "$REPO" || exit 1
  MEGASPEED_TENANTS_DIR="$BENCH/tcache" MEGASPEED_QUIET=1 \
    "$PY" -m server --port $PORT > "$BENCH/tcache/fp-${LABEL}-${tenant}.log" 2>&1 &
  pid=$!
  for _ in $(seq 60); do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/health?tenant=$tenant" 2>/dev/null)
    [ "$code" = "200" ] && break
    sleep 0.5
  done
  "$PY" "$BENCH/fingerprint.py" $PORT "$tenant" "$BENCH/tcache/fp-${LABEL}-${tenant}.json"
  kill $pid 2>/dev/null
  wait $pid 2>/dev/null
done

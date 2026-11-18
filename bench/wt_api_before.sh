#!/bin/bash
# API-level timings for the *unmodified* main repo server on :8791 (before).
B=http://127.0.0.1:8791
one() { # label, path  (one warmup, one measured run)
  curl -s -o /dev/null "$B$2"
  t=$(curl -s -o /dev/null -w '%{time_total}' "$B$2")
  printf '%-46s %8s s\n' "$1" "$t"
}

one "/api/queues person (default buckets)" "/api/queues?level=person&tenant=big40"
one "/api/insights (warm)" "/api/insights?tenant=big40"
one "/api/events?limit=200000" "/api/events?limit=200000&tenant=big40"

echo "--- /api/ticket"
curl -s -o /dev/null -w 'cold   %{time_total}s\n' "$B/api/ticket/DEL-100?tenant=big40"
curl -s -o /dev/null -w 'warm   %{time_total}s\n' "$B/api/ticket/DEL-100?tenant=big40"
curl -s -o /dev/null -w 'warm   %{time_total}s\n' "$B/api/ticket/DEL-100?tenant=big40"

echo "--- sizes (no compression in the before build)"
for path in "/api/insights?tenant=big40" "/api/events?limit=200000&tenant=big40" "/api/sprints?tenant=big40"; do
  printf '%-46s raw %10s gzip %9s bytes\n' "$path" \
    "$(curl -s -o /dev/null -w '%{size_download}' "$B$path")" \
    "$(curl -s -o /dev/null -w '%{size_download}' -H 'Accept-Encoding: gzip' "$B$path")"
done

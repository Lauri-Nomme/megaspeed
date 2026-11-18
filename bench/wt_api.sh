#!/bin/bash
# API-level timings for the perf-analysis worktree server on :8790.
B=http://127.0.0.1:8790
q() { # label, path
  local label="$1" path="$2"
  # cold-ish: one warmup, then median of 3 via curl
  curl -s -o /dev/null "$B$path"
  local times=()
  for i in 1 2 3; do
    t=$(curl -s -o /dev/null -w '%{time_total}' "$B$path")
    times+=("$t")
  done
  printf '%-46s %8s %8s %8s s\n' "$label" "${times[0]}" "${times[1]}" "${times[2]}"
}
size() { # label, path
  local raw gz
  raw=$(curl -s -o /dev/null -w '%{size_download}' "$B$2")
  gz=$(curl -s -o /dev/null -w '%{size_download}' -H 'Accept-Encoding: gzip' "$B$2")
  printf '%-46s raw %10s  gzip %9s bytes\n' "$1" "$raw" "$gz"
}

q "/api/queues person (default buckets)" "/api/queues?level=person&tenant=big40"
q "/api/queues person bucket_hours=1" "/api/queues?level=person&bucket_hours=1&tenant=big40"
q "/api/queues queue bucket_hours=1" "/api/queues?level=queue&bucket_hours=1&tenant=big40"
q "/api/insights (warm)" "/api/insights?tenant=big40"
q "/api/events?limit=200000" "/api/events?limit=200000&tenant=big40"

echo "--- /api/ticket (cold = first request after a store-stamp change)"
curl -s -o /dev/null -w 'cold   %{time_total}s\n' "$B/api/ticket/DEL-100?tenant=big40"
for i in 1 2 3; do
  curl -s -o /dev/null -w 'warm   %{time_total}s\n' "$B/api/ticket/DEL-100?tenant=big40"
done
curl -s -o /dev/null -w 'other key %{time_total}s\n' "$B/api/ticket/DEL-100100?tenant=big40"

echo "--- payload sizes"
size "/api/insights" "/api/insights?tenant=big40"
size "/api/events?limit=200000" "/api/events?limit=200000&tenant=big40"
size "/api/ticket/DEL-100" "/api/ticket/DEL-100?tenant=big40"
size "/api/sprints" "/api/sprints?tenant=big40"
size "/api/queues person 1h" "/api/queues?level=person&bucket_hours=1&tenant=big40"

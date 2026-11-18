"""Fingerprint the analysis payloads so a cache change can be proven byte-identical.

Usage: fingerprint.py <port> <tenant> <out.json>

Every ticket key, `/api/insights` (team + person) and `/api/sprints` are
fetched once (in a fresh server process) and their sha256 + length recorded.
"""
import hashlib
import json
import sys
import urllib.request

PORT, TENANT, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
BASE = f"http://127.0.0.1:{PORT}"


def stable(body):
    """The payload minus the one field that is a clock reading (`generated_at`)."""
    try:
        doc = json.loads(body)
    except ValueError:
        return body
    if isinstance(doc, dict):
        doc.pop("generated_at", None)
    return json.dumps(doc, sort_keys=True).encode()


def fetch(path):
    with urllib.request.urlopen(f"{BASE}{path}{'&' if '?' in path else '?'}tenant={TENANT}",
                                timeout=600) as r:
        return r.read()


rows = json.loads(fetch("/api/objects?type=STORY&limit=2000"))
keys = []
for row in rows:
    for key in row.get("ticket_keys") or []:
        if key not in keys:
            keys.append(key)

out = {}
for key in keys:
    body = fetch(f"/api/ticket/{key}")
    out[f"/api/ticket/{key}"] = [len(body), hashlib.sha256(body).hexdigest()]
for path in ("/api/insights?level=team", "/api/insights?level=person", "/api/sprints",
             "/api/delivery", "/api/blocking", "/api/queues?level=person"):
    body = fetch(path)
    out[path] = [len(body), hashlib.sha256(stable(body)).hexdigest()]

with open(OUT, "w") as fh:
    json.dump(out, fh, indent=1, sort_keys=True)
print(f"{TENANT}: {len(out)} payloads -> {OUT}")

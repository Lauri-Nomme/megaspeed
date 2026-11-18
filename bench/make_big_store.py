"""Replicate the del-combined store K times into a synthetic stress store.

Preserves every identifier's *shape* (repo slug, ticket key, PR id, sha, ref
digest) while making it unique per copy, so the analysis sees K independent
copies of the same 100-story / 131-PR corpus with the same distributions.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

import duckdb

_BENCH = Path(__file__).resolve().parent
SRC = str(_BENCH.parent / "data" / "del-combined.duckdb")
COPIES = int(sys.argv[1]) if len(sys.argv) > 1 else 10
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else _BENCH / "stores" / f"big{COPIES}.duckdb"

TICKET = re.compile(r"DEL-(\d+)")
PRNUM = re.compile(r"#(\d+)")
REPO = re.compile(r"DEL/(delivery-(?:api|web))")


def sha_for(k: int, value: str) -> str:
    return hashlib.sha256(f"{k}:{value}".encode()).hexdigest()[:40]


COMMIT = re.compile(r"^COMMIT:([0-9a-f]{7,40})$")


def sub(k: int, text: str) -> str:
    if k == 0:
        return text
    m = COMMIT.match(text)
    if m:
        return "COMMIT:" + sha_for(k, m.group(1))
    text = REPO.sub(lambda m: f"DELC{k}/{m.group(1)}", text)
    text = TICKET.sub(lambda m: f"DEL-{int(m.group(1)) + k * 10000}", text)
    text = PRNUM.sub(lambda m: f"#{int(m.group(1)) + k * 100000}", text)
    return text


REF_KEYS = {"source_ref", "target_ref", "merge_commit", "head_commit"}
SHA = re.compile(r"^[0-9a-f]{7,40}$")


def attrs(k: int, raw):
    if not raw:
        return raw
    data = json.loads(raw) if isinstance(raw, str) else raw

    def walk(node, key=None):
        if isinstance(node, dict):
            return {kk: walk(vv, kk) for kk, vv in node.items()}
        if isinstance(node, list):
            return [walk(vv, key) for vv in node]
        if isinstance(node, str):
            if key in REF_KEYS and SHA.fullmatch(node):
                return sha_for(k, node)
            return sub(k, node)
        return node

    return json.dumps(walk(data), separators=(",", ":"), sort_keys=True)


def keys(k, arr):
    return None if arr is None else [sub(k, v) for v in arr]


con = duckdb.connect(SRC, read_only=True)
events = con.execute("select * from events").fetchall()
relations = con.execute("select * from event_objects").fetchall()
objects = con.execute("select object_type,object_id,repo,pr_id,ticket_keys,actor,state,created_at,closed_at,head_commit,message_class,attributes from objects").fetchall()
runs = con.execute("select run_id,source,as_of,source_watermark,classifier_version,ticket_keys,prs_seen,prs_kept,prs_dropped,events_written,events_skipped,issues_seen,issues_kept,issues_skipped from ingest_runs").fetchall()
con.close()

OUT.unlink(missing_ok=True)
out = duckdb.connect(str(OUT))
out.execute((_BENCH.parent / "ingest" / "schema.sql").read_text())
out.execute("ALTER TABLE objects ADD COLUMN IF NOT EXISTS attributes JSON")

for k in range(COPIES):
    ev = [
        (sub(k, r[0]), r[1], r[2], r[3], sub(k, r[4]) if r[4] else r[4],
         (r[5] + k * 100000) if r[5] is not None else None,
         attrs(k, r[6]), r[7], r[8], r[9])
        for r in events
    ]
    rel = [
        (sub(k, r[0]), r[1], sub(k, r[2])) for r in relations
    ]
    ob = [
        (r[0], sub(k, r[1]), sub(k, r[2]) if r[2] else r[2],
         (r[3] + k * 100000) if r[3] is not None else None,
         keys(k, r[4]), r[5], r[6], r[7], r[8],
         sha_for(k, r[9]) if r[9] and SHA.fullmatch(r[9]) else r[9],
         r[10], attrs(k, r[11]))
        for r in objects
    ]
    out.executemany(
        "insert into events values (?,?,?,?,?,?,CAST(? AS JSON),?,?,?)", ev)
    out.executemany("insert into event_objects values (?,?,?)", rel)
    out.executemany(
        "insert or ignore into objects values (?,?,?,?,?,?,?,?,?,?,?,CAST(? AS JSON))", ob)
    for r in runs:
        out.execute(
            "insert or replace into ingest_runs values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{r[0]}-c{k}", r[1], r[2], r[3], r[4], keys(k, r[5]), r[6], r[7],
             r[8], r[9], r[10], r[11], r[12], r[13]))

n = out.execute("select count(*) from events").fetchone()[0]
p = out.execute("select count(*) from objects where object_type='PR'").fetchone()[0]
s = out.execute("select count(*) from objects where object_type='STORY'").fetchone()[0]
print(f"{OUT}: {n} events, {p} PRs, {s} stories ({COPIES} copies)")
out.close()

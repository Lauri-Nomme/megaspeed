"""DuckDB store: schema, guarded writes, and the only read path the API uses.

Three defences, in order of strength:

1. **Allowlist** — every table and column must be declared in ``ALLOWED`` below.
   Adding a free-text column fails :func:`verify` (and ingest calls it).
2. **Shape** — no stored string value may contain whitespace. Keys, pseudonyms,
   hashes, repo slugs and enum labels do not; prose does.
3. **Canary** — the fixtures plant unique ``zzcanary…`` tokens in every
   human-authored field. If one appears in the store, ingest fails loudly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from . import taxonomy

SCHEMA_SQL = Path(__file__).with_name("schema.sql")

ALLOWED: dict[str, set[str]] = {
    "events": {
        "event_key", "ts", "activity", "actor", "repo", "pr_id",
        "attributes", "classifier_version", "content_hash", "as_of",
    },
    "event_objects": {"event_key", "object_type", "object_id"},
    "objects": {
        "object_type", "object_id", "repo", "pr_id", "ticket_keys", "actor",
        "state", "created_at", "closed_at", "head_commit", "message_class",
    },
    "ingest_runs": {
        "run_id", "source", "as_of", "source_watermark", "classifier_version",
        "prs_seen", "prs_kept", "prs_dropped", "events_written", "events_skipped",
    },
}

CANARY = "zzcanary"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime | int | float | str) -> datetime:
    """Accept epoch-ms, epoch-s, ISO-8601 or datetime; return naive UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Bitbucket DC uses epoch milliseconds.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("empty timestamp")
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(ts: datetime) -> str:
    return ts.replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path: str | Path, read_only: bool = False):
    return duckdb.connect(str(path), read_only=read_only)


def ensure_schema(con) -> None:
    con.execute(SCHEMA_SQL.read_text())


# --------------------------------------------------------------------------- #
# writes (used by ingest only)
# --------------------------------------------------------------------------- #
INSERT_EVENT = """
INSERT OR IGNORE INTO events
    (event_key, ts, activity, actor, repo, pr_id, attributes, classifier_version, content_hash, as_of)
VALUES (?, ?, ?, ?, ?, ?, CAST(? AS JSON), ?, ?, ?)
"""

UPDATE_EVENT = """
UPDATE events SET
    ts = ?, activity = ?, actor = ?, repo = ?, pr_id = ?,
    attributes = CAST(? AS JSON), classifier_version = ?, content_hash = ?, as_of = ?
WHERE event_key = ?
"""

INSERT_REL = "INSERT OR IGNORE INTO event_objects (event_key, object_type, object_id) VALUES (?, ?, ?)"

INSERT_OBJECT = """
INSERT OR REPLACE INTO objects
    (object_type, object_id, repo, pr_id, ticket_keys, actor, state, created_at, closed_at, head_commit, message_class)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_RUN = """
INSERT OR REPLACE INTO ingest_runs
    (run_id, source, as_of, source_watermark, classifier_version,
     prs_seen, prs_kept, prs_dropped, events_written, events_skipped)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _json_attr(value) -> dict:
    if isinstance(value, str):
        return json.loads(value or "{}")
    return dict(value or {})


def write_events(con, events: list[dict], relations: list[tuple[str, str, str]]) -> tuple[int, int]:
    """Upsert events by natural key; return ``(inserted, updated)``.

    A row is only rewritten when its content actually changed. That gives two
    things the old ``INSERT OR IGNORE`` could not:

    * derived attributes computed after the fact (``approvals_lost``) converge
      on re-ingest instead of being frozen at whatever run saw the row first;
    * bumping ``classifier_version`` and replaying the same raw source
      re-derives every existing row (`prd/02`: reproducible/tunable).
    """
    if not events:
        if relations:
            con.executemany(INSERT_REL, relations)
        return 0, 0

    existing: dict[str, tuple] = {}
    keys = [e["event_key"] for e in events]
    for start in range(0, len(keys), 500):
        chunk = keys[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in con.execute(
            "SELECT event_key, ts, activity, actor, repo, pr_id, CAST(attributes AS VARCHAR),"
            f" classifier_version, content_hash FROM events WHERE event_key IN ({placeholders})",
            chunk,
        ).fetchall():
            existing[row[0]] = row

    inserts: list[dict] = []
    updates: list[dict] = []
    for event in events:
        row = existing.get(event["event_key"])
        if row is None:
            inserts.append(event)
            continue
        changed = (
            row[1] != event["ts"]
            or row[2] != event["activity"]
            or row[3] != event["actor"]
            or row[4] != event["repo"]
            or row[5] != event["pr_id"]
            or _json_attr(row[6]) != event["attributes"]
            or row[7] != event["classifier_version"]
            or row[8] != event["content_hash"]
        )
        if changed:
            updates.append(event)

    if inserts:
        con.executemany(
            INSERT_EVENT,
            [
                (
                    e["event_key"], e["ts"], e["activity"], e["actor"], e["repo"], e["pr_id"],
                    json.dumps(e["attributes"], separators=(",", ":"), sort_keys=True),
                    e["classifier_version"], e["content_hash"], e["as_of"],
                )
                for e in inserts
            ],
        )
    if updates:
        con.executemany(
            UPDATE_EVENT,
            [
                (
                    e["ts"], e["activity"], e["actor"], e["repo"], e["pr_id"],
                    json.dumps(e["attributes"], separators=(",", ":"), sort_keys=True),
                    e["classifier_version"], e["content_hash"], e["as_of"], e["event_key"],
                )
                for e in updates
            ],
        )
        # Objects attached to a rewritten event may have changed too (e.g. a
        # merge commit that only a later ingest knows about).
        stale = [e["event_key"] for e in updates]
        for start in range(0, len(stale), 500):
            chunk = stale[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            chunk_set = set(chunk)
            con.execute(
                f"DELETE FROM event_objects WHERE event_key IN ({placeholders})", chunk
            )
            con.executemany(
                INSERT_REL,
                [rel for rel in relations if rel[0] in chunk_set],
            )

    rewritten = {e["event_key"] for e in updates}
    fresh_relations = [rel for rel in relations if rel[0] not in rewritten]
    if fresh_relations:
        con.executemany(INSERT_REL, fresh_relations)
    return len(inserts), len(updates)


def write_objects(con, objects: list[dict]) -> None:
    if objects:
        con.executemany(
            INSERT_OBJECT,
            [
                (
                    o["object_type"], o["object_id"], o.get("repo"), o.get("pr_id"),
                    o.get("ticket_keys") or [], o.get("actor"), o.get("state"),
                    o.get("created_at"), o.get("closed_at"), o.get("head_commit"),
                    o.get("message_class"),
                )
                for o in objects
            ],
        )


def write_run(con, run: dict) -> None:
    con.execute(
        INSERT_RUN,
        (
            run["run_id"], run["source"], run["as_of"], run["source_watermark"],
            run["classifier_version"], run["prs_seen"], run["prs_kept"],
            run["prs_dropped"], run["events_written"], run["events_skipped"],
        ),
    )


# --------------------------------------------------------------------------- #
# reads (shared by the API and the verifier)
# --------------------------------------------------------------------------- #
def list_events(con, start: datetime | None = None, end: datetime | None = None, limit: int | None = None) -> list[dict]:
    where, params = [], []
    if start is not None:
        where.append("ts >= ?")
        params.append(start)
    if end is not None:
        where.append("ts <= ?")
        params.append(end)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = con.execute(
        "SELECT event_key, ts, activity, actor, repo, pr_id, attributes, as_of FROM events"
        + clause
        + " ORDER BY ts ASC, event_key ASC"
        + (f" LIMIT {int(limit)}" if limit else ""),
        params,
    ).fetchall()
    if not rows:
        return []
    keys = [r[0] for r in rows]
    rel: dict[str, list[str]] = {k: [] for k in keys}
    placeholders = ",".join("?" * len(keys))
    for key, _otype, oid in con.execute(
        f"SELECT event_key, object_type, object_id FROM event_objects WHERE event_key IN ({placeholders})"
        " ORDER BY object_type, object_id",
        keys,
    ).fetchall():
        rel[key].append(oid)
    out = []
    for key, ts, activity, actor, repo, pr_id, attributes, as_of in rows:
        out.append(
            {
                "key": key,
                "ts": iso(ts),
                "activity": activity,
                "actor": actor,
                "objects": rel.get(key, []),
                "attributes": json.loads(attributes) if isinstance(attributes, str) else (attributes or {}),
                "repo": repo,
                "pr_id": pr_id,
                "as_of": iso(as_of) if as_of else None,
            }
        )
    return out


def list_objects(con, object_type: str | None = None) -> list[dict]:
    sql = (
        "SELECT object_type, object_id, repo, pr_id, ticket_keys, actor, state,"
        " created_at, closed_at, head_commit, message_class FROM objects"
    )
    params: list = []
    if object_type:
        sql += " WHERE object_type = ?"
        params.append(object_type)
    sql += " ORDER BY object_type, object_id"
    out = []
    for row in con.execute(sql, params).fetchall():
        (otype, oid, repo, pr_id, keys, actor, state, created, closed, head, mclass) = row
        out.append(
            {
                "object_type": otype,
                "object_id": oid,
                "repo": repo,
                "pr_id": pr_id,
                "ticket_keys": list(keys or []),
                "actor": actor,
                "state": state,
                "created_at": iso(created) if created else None,
                "closed_at": iso(closed) if closed else None,
                "head_commit": head,
                "message_class": mclass,
            }
        )
    return out


def summary(con) -> dict:
    events, first, last = con.execute("SELECT count(*), min(ts), max(ts) FROM events").fetchone()
    prs = con.execute("SELECT count(*) FROM objects WHERE object_type = 'PR'").fetchone()[0]
    commits = con.execute("SELECT count(*) FROM objects WHERE object_type = 'COMMIT'").fetchone()[0]
    run = con.execute(
        "SELECT as_of, source, prs_seen, prs_kept, prs_dropped FROM ingest_runs ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    return {
        "events": int(events or 0),
        "prs": int(prs or 0),
        "commits": int(commits or 0),
        "first_ts": iso(first) if first else None,
        "last_ts": iso(last) if last else None,
        "as_of": iso(run[0]) if run else None,
        "source": run[1] if run else None,
        "prs_seen": int(run[2]) if run else 0,
        "prs_kept": int(run[3]) if run else 0,
        "prs_dropped": int(run[4]) if run else 0,
    }


# --------------------------------------------------------------------------- #
# verification — the pseudonymisation gate
# --------------------------------------------------------------------------- #
def _string_columns(con) -> list[tuple[str, str, str]]:
    """All string-ish columns, including array element columns."""
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()]
    cols = []
    for table in tables:
        for name, dtype in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'main' AND table_name = ?",
            [table],
        ).fetchall():
            if dtype in ("VARCHAR", "JSON", "VARCHAR[]"):
                cols.append((table, name, dtype))
    return cols


def _scan(con, table: str, column: str, dtype: str, predicate: str, params: list) -> int:
    """Count values in a (possibly array) string column matching a SQL predicate
    over the alias ``v``."""
    if dtype == "VARCHAR[]":
        source = f'SELECT unnest("{column}") AS v FROM "{table}"'
    else:
        source = f'SELECT CAST("{column}" AS VARCHAR) AS v FROM "{table}"'
    sql = f"SELECT count(*) FROM ({source}) WHERE v IS NOT NULL AND {predicate}"
    return int(con.execute(sql, params).fetchone()[0])


def verify(con) -> dict:
    """Return a report; ``ok`` is False if any part of the contract is broken."""
    problems: list[str] = []

    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()}
    for table in sorted(tables):
        if table not in ALLOWED:
            problems.append(f"undeclared table: {table}")
            continue
        columns = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'main' AND table_name = ?",
            [table],
        ).fetchall()}
        extra = columns - ALLOWED[table]
        missing = ALLOWED[table] - columns
        if extra:
            problems.append(f"{table}: columns outside the allowlist: {sorted(extra)}")
        if missing:
            problems.append(f"{table}: missing expected columns: {sorted(missing)}")

    # No whitespace anywhere in a stored string: prose has spaces, identifiers
    # (keys, pseudonyms, hashes, repo slugs, enum labels) do not.
    for table, column, dtype in _string_columns(con):
        bad = _scan(con, table, column, dtype, r"regexp_matches(v, '\s')", [])
        if bad:
            problems.append(f"{table}.{column}: {bad} value(s) contain whitespace")
        leaked = _scan(con, table, column, dtype, "lower(v) LIKE ?", [f"%{CANARY}%"])
        if leaked:
            problems.append(f"{table}.{column}: {leaked} canary value(s) leaked")

    # Closed vocabularies actually hold.
    for (activity,) in con.execute("SELECT DISTINCT activity FROM events").fetchall():
        if activity not in taxonomy.ACTIVITIES:
            problems.append(f"activity outside vocabulary: {activity!r}")
    for (attrs,) in con.execute("SELECT DISTINCT attributes FROM events").fetchall():
        try:
            payload = json.loads(attrs) if isinstance(attrs, str) else (attrs or {})
            taxonomy.check_attributes(payload)
        except (taxonomy.TaxonomyError, json.JSONDecodeError) as exc:
            problems.append(f"invalid attributes: {exc}")
    for (otype,) in con.execute("SELECT DISTINCT object_type FROM objects").fetchall():
        if otype not in taxonomy.OBJECT_TYPES:
            problems.append(f"object type outside vocabulary: {otype!r}")

    return {
        "ok": not problems,
        "problems": problems,
        "tables": {t: sorted(ALLOWED.get(t, set())) for t in sorted(tables)},
        "summary": summary(con),
    }

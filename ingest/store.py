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
import re
from datetime import datetime
from pathlib import Path

import duckdb

from . import taxonomy
from .config import DEFAULT_KEY_SHAPE, DEFAULT_TICKET_PROJECTS
from .timeutil import iso, to_naive_utc, utcnow  # re-exported for existing importers

__all__ = [
    "ALLOWED", "CANARY", "connect", "ensure_schema", "iso", "list_events",
    "list_objects", "object_shape_problems", "summary", "to_naive_utc", "utcnow",
    "verify", "write_events", "write_objects", "write_run",
]

SCHEMA_SQL = Path(__file__).with_name("schema.sql")

# Pseudonym shape: two initials (prd/02) plus an optional numeric collision
# suffix, or a synthetic actor (prd/03: CI, calendar, bot:<name>).
ACTOR_RE = r"^(?:[A-Z][a-z]?[A-Z][a-z]?\d*|system|CI|calendar|bot:[A-Za-z0-9_.-]+)$"
# content_hash is a blake2b digest (digest_size=8) in hex, nothing else.
CONTENT_HASH_RE = r"^[0-9a-f]{16}$"
REPO_RE = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
# A commit hash (full or abbreviated) -- ``objects.head_commit``.
HEAD_COMMIT_RE = r"^[0-9a-f]{7,40}$"
# ``event_key`` is ``project/repo#pr_id:natural`` for Bitbucket and
# ``KEY:natural`` for Jira (``DEL-100:changelog:10190:status:0``). The shape is
# owned by :mod:`ingest.taxonomy` (both sides of the airgap need it, and the
# scrape side may not import this module); re-exported here so the import gate
# and the verifier share one definition.
EVENT_KEY_RE = taxonomy.EVENT_KEY_RE
# ``ingest_runs.run_id``: one token, no whitespace (``fixture-2026...-abcdef``).
RUN_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$"
# The sources this store may hold. A source name is a closed label, not free
# text: it is the provenance column of ``ingest_runs``.
SOURCES = ("fixture", "bitbucket-dc", "jira-fixture", "jira-dc")
OBJECT_ID_PREFIX = {
    "PR": "PR:",
    "COMMIT": "COMMIT:",
    "TICKET": "TICKET:",
    "PERSON": "PERSON:",
    "PRREV": "PRREV:",
    "THREAD": "THREAD:",
    "EPIC": "EPIC:",
    "STORY": "STORY:",
    "SPRINT": "SPRINT:",
}
# Per-object-type state vocabulary (prd/03).
OBJECT_STATE_BY_TYPE = {
    "PR": taxonomy.OBJECT_STATE,
    "THREAD": taxonomy.THREAD_STATE,
    "EPIC": taxonomy.ISSUE_STATE,
    "STORY": taxonomy.ISSUE_STATE,
    "SPRINT": taxonomy.SPRINT_STATE,
}
# Object types whose id is a ticket key (the PRD allows keys to cross, prd/02).
KEYED_OBJECT_TYPES = ("TICKET", "EPIC", "STORY")

ALLOWED: dict[str, set[str]] = {
    "events": {
        "event_key", "ts", "activity", "actor", "repo", "pr_id",
        "attributes", "classifier_version", "content_hash", "as_of",
    },
    "event_objects": {"event_key", "object_type", "object_id"},
    "objects": {
        "object_type", "object_id", "repo", "pr_id", "ticket_keys", "actor",
        "state", "created_at", "closed_at", "head_commit", "message_class",
        "attributes",
    },
    "ingest_runs": {
        "run_id", "source", "as_of", "source_watermark", "classifier_version",
        "prs_seen", "prs_kept", "prs_dropped", "events_written", "events_skipped",
        "issues_seen", "issues_kept", "issues_skipped",
    },
}

CANARY = "zzcanary"


def connect(path: str | Path, read_only: bool = False):
    return duckdb.connect(str(path), read_only=read_only)


def ensure_schema(con) -> None:
    con.execute(SCHEMA_SQL.read_text())
    # Additive migrations for stores created by earlier builds: the schema file
    # is the contract, so a new allowlisted column must exist everywhere.
    con.execute("ALTER TABLE objects ADD COLUMN IF NOT EXISTS attributes JSON")
    for column in ("issues_seen", "issues_kept", "issues_skipped"):
        con.execute(f"ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS {column} INTEGER")


# --------------------------------------------------------------------------- #
# writes (used by ingest only)
# --------------------------------------------------------------------------- #
# Rows are staged as a DuckDB relation (``con.values(*rows)``) and merged with
# one ``INSERT ... SELECT`` / ``UPDATE ... FROM`` per batch. The Python client's
# ``executemany`` runs one statement per row (and ``INSERT OR IGNORE`` against a
# primary key scans for every row), which made import ~9 ms/event (NFR-1).
# ``colN`` is the relation's positional column name; the explicit casts keep the
# inferred type stable when the first row of a batch has a NULL optional field.
INSERT_EVENT = """
INSERT INTO events
    (event_key, ts, activity, actor, repo, pr_id, attributes, classifier_version, content_hash, as_of)
SELECT col0, col1, col2, col3, CAST(col4 AS VARCHAR), CAST(col5 AS BIGINT),
       CAST(col6 AS JSON), col7, CAST(col8 AS VARCHAR), col9
FROM incoming
"""

UPDATE_EVENT = """
UPDATE events SET
    ts = s.col1, activity = s.col2, actor = s.col3,
    repo = CAST(s.col4 AS VARCHAR), pr_id = CAST(s.col5 AS BIGINT),
    attributes = CAST(s.col6 AS JSON), classifier_version = s.col7,
    content_hash = CAST(s.col8 AS VARCHAR), as_of = s.col9
FROM incoming s
WHERE events.event_key = s.col0
"""

INSERT_REL = """
INSERT OR IGNORE INTO event_objects (event_key, object_type, object_id)
SELECT col0, col1, col2 FROM relations_rel
"""

INSERT_OBJECT = """
INSERT OR REPLACE INTO objects
    (object_type, object_id, repo, pr_id, ticket_keys, actor, state, created_at, closed_at, head_commit,
     message_class, attributes)
SELECT col0, col1, CAST(col2 AS VARCHAR), CAST(col3 AS BIGINT), CAST(col4 AS VARCHAR[]),
       CAST(col5 AS VARCHAR), CAST(col6 AS VARCHAR), CAST(col7 AS TIMESTAMP),
       CAST(col8 AS TIMESTAMP), CAST(col9 AS VARCHAR), CAST(col10 AS VARCHAR),
       CAST(col11 AS JSON)
FROM objects_in
"""

#: Rows per staged relation. Big enough that the per-statement cost disappears,
#: small enough that a 1M-event bundle does not build one enormous VALUES list.
BATCH_ROWS = 5_000


def _batches(rows: list, size: int = BATCH_ROWS):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]

INSERT_RUN = """
INSERT OR REPLACE INTO ingest_runs
    (run_id, source, as_of, source_watermark, classifier_version,
     prs_seen, prs_kept, prs_dropped, events_written, events_skipped,
     issues_seen, issues_kept, issues_skipped)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        for batch in _batches(relations):
            relations_rel = con.values(*batch)
            con.execute(INSERT_REL)
        return 0, 0

    existing: dict[str, tuple] = {}
    keys = [e["event_key"] for e in events]
    for start in range(0, len(keys), BATCH_ROWS):
        chunk = keys[start : start + BATCH_ROWS]
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
        for batch in _batches(inserts):
            incoming = con.values(
                *[
                    (
                        e["event_key"], e["ts"], e["activity"], e["actor"], e["repo"], e["pr_id"],
                        json.dumps(e["attributes"], separators=(",", ":"), sort_keys=True),
                        e["classifier_version"], e["content_hash"], e["as_of"],
                    )
                    for e in batch
                ]
            )
            con.execute(INSERT_EVENT)
    if updates:
        for batch in _batches(updates):
            incoming = con.values(
                *[
                    (
                        e["event_key"], e["ts"], e["activity"], e["actor"], e["repo"], e["pr_id"],
                        json.dumps(e["attributes"], separators=(",", ":"), sort_keys=True),
                        e["classifier_version"], e["content_hash"], e["as_of"],
                    )
                    for e in batch
                ]
            )
            con.execute(UPDATE_EVENT)
        # Objects attached to a rewritten event may have changed too (e.g. a
        # merge commit that only a later ingest knows about).
        stale = [e["event_key"] for e in updates]
        for start in range(0, len(stale), BATCH_ROWS):
            chunk = stale[start : start + BATCH_ROWS]
            placeholders = ",".join("?" * len(chunk))
            chunk_set = set(chunk)
            con.execute(
                f"DELETE FROM event_objects WHERE event_key IN ({placeholders})", chunk
            )
            for batch in _batches([rel for rel in relations if rel[0] in chunk_set]):
                relations_rel = con.values(*batch)
                con.execute(INSERT_REL)

    rewritten = {e["event_key"] for e in updates}
    fresh_relations = [rel for rel in relations if rel[0] not in rewritten]
    if fresh_relations:
        for batch in _batches(fresh_relations):
            relations_rel = con.values(*batch)
            con.execute(INSERT_REL)
    return len(inserts), len(updates)


def write_objects(con, objects: list[dict]) -> None:
    if not objects:
        return
    for batch in _batches(objects):
        objects_in = con.values(
            *[
                (
                    o["object_type"], o["object_id"], o.get("repo"), o.get("pr_id"),
                    o.get("ticket_keys") or [], o.get("actor"), o.get("state"),
                    o.get("created_at"), o.get("closed_at"), o.get("head_commit"),
                    o.get("message_class"),
                    json.dumps(o.get("attributes") or {}, separators=(",", ":"), sort_keys=True),
                )
                for o in batch
            ]
        )
        con.execute(INSERT_OBJECT)


def write_run(con, run: dict) -> None:
    con.execute(
        INSERT_RUN,
        (
            run["run_id"], run["source"], run["as_of"], run["source_watermark"],
            run["classifier_version"], run["prs_seen"], run["prs_kept"],
            run["prs_dropped"], run["events_written"], run["events_skipped"],
            # Jira sources count issues where Bitbucket counts PRs; a run only
            # fills the half that belongs to its source (prd/11 §8: per-source
            # cursors and per-source counters).
            int(run.get("issues_seen") or 0),
            int(run.get("issues_kept") or 0),
            int(run.get("issues_skipped") or 0),
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
        " created_at, closed_at, head_commit, message_class, CAST(attributes AS VARCHAR) FROM objects"
    )
    params: list = []
    if object_type:
        sql += " WHERE object_type = ?"
        params.append(object_type)
    sql += " ORDER BY object_type, object_id"
    out = []
    for row in con.execute(sql, params).fetchall():
        (otype, oid, repo, pr_id, keys, actor, state, created, closed, head, mclass, attrs) = row
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
                "attributes": _json_attr(attrs),
            }
        )
    return out


def summary(con) -> dict:
    events, first, last = con.execute("SELECT count(*), min(ts), max(ts) FROM events").fetchone()
    counts = {
        row[0]: int(row[1])
        for row in con.execute(
            "SELECT object_type, count(*) FROM objects GROUP BY object_type"
        ).fetchall()
    }
    run = con.execute(
        "SELECT as_of, source, prs_seen, prs_kept, prs_dropped FROM ingest_runs ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    return {
        "events": int(events or 0),
        "prs": counts.get("PR", 0),
        "commits": counts.get("COMMIT", 0),
        # Jira side (prd/03): the ticket layer the PRs join to.
        "epics": counts.get("EPIC", 0),
        "stories": counts.get("STORY", 0),
        "sprints": counts.get("SPRINT", 0),
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


def object_shape_problems(
    otype: str,
    oid: str,
    state: str | None,
    key_shape: str = DEFAULT_KEY_SHAPE,
    key_set: tuple[str, ...] = (),
) -> list[str]:
    """Shape problems for one ``objects`` row (shared by verify and the gate)."""
    problems: list[str] = []
    prefix = OBJECT_ID_PREFIX.get(otype)
    if prefix and not str(oid).startswith(prefix):
        problems.append(f"object {oid!r} does not match its type {otype!r}")
    elif otype == "PR" and not re.search(r"#\d+$", str(oid)):
        problems.append(f"PR object id without a numeric id: {oid!r}")
    elif otype == "COMMIT" and not re.fullmatch(r"COMMIT:[0-9a-f]{7,40}", str(oid)):
        problems.append(f"COMMIT object id is not a hash: {oid!r}")
    elif otype in KEYED_OBJECT_TYPES:
        key = str(oid).partition(":")[2]
        if not re.fullmatch(key_shape, key):
            problems.append(f"{otype} object id is not a key: {oid!r}")
        elif key_set and key.upper() not in key_set:
            problems.append(
                f"{otype} object id is outside the configured ticket keys: {oid!r}"
            )
    elif otype == "PERSON" and not re.fullmatch(ACTOR_RE, str(oid).partition(":")[2]):
        problems.append(f"PERSON object id is not a pseudonym: {oid!r}")
    elif otype in ("PRREV", "THREAD") and not re.fullmatch(
        rf"{otype}:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+:\d+", str(oid)
    ):
        problems.append(f"{otype} object id has an unexpected shape: {oid!r}")
    elif otype == "SPRINT" and not re.fullmatch(
        r"SPRINT:[A-Z][A-Z0-9_]{0,9}#\d+", str(oid)
    ):
        problems.append(f"SPRINT object id has an unexpected shape: {oid!r}")
    allowed_states = OBJECT_STATE_BY_TYPE.get(otype)
    if allowed_states is not None and state is not None and state not in allowed_states:
        problems.append(
            f"{otype} {oid!r}: state {state!r} outside {sorted(allowed_states)}"
        )
    return problems


def verify(con, key_shape: str = DEFAULT_KEY_SHAPE,
           ticket_projects: tuple[str, ...] = DEFAULT_TICKET_PROJECTS,
           ticket_keys: tuple[str, ...] = ()) -> dict:
    """Return a report; ``ok`` is False if any part of the contract is broken.

    The guard is deliberately about *content class*, not only container shape:
    every string column must hold something of a known shape (a pseudonym, a
    hex hash, a repo slug, a key allowed by the configured scope, a closed
    label). The old whitespace check alone blessed camelCase names, prose-like
    hashes and garbage enum values.

    ``ticket_keys`` is the explicit key set (prd/18): when non-empty it replaces
    the prefix check, so a store built with ``MEGASPEED_TICKET_KEYS=DEL-100`` is
    rejected if it holds ``DEL-101`` (and vice versa).
    """
    problems: list[str] = []
    key_set = sorted({str(k).strip().upper() for k in ticket_keys or () if str(k).strip()})

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
    object_columns = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'main' AND table_name = 'objects'"
        ).fetchall()
    }
    if "attributes" in object_columns:
        for (attrs,) in con.execute(
            "SELECT DISTINCT CAST(attributes AS VARCHAR) FROM objects"
        ).fetchall():
            try:
                payload = json.loads(attrs) if isinstance(attrs, str) else (attrs or {})
                taxonomy.check_object_attributes(payload)
            except (taxonomy.TaxonomyError, json.JSONDecodeError) as exc:
                problems.append(f"invalid object attributes: {exc}")

    # A pseudonym must not be a bot in one event and a human in another: the
    # actor dimension is a property of the account, not of the activity.
    kinds: dict[str, set[str]] = {}
    for actor, kind in con.execute(
        "SELECT actor, CAST(attributes->>'actor_kind' AS VARCHAR) FROM events"
        " WHERE attributes->>'actor_kind' IS NOT NULL"
    ).fetchall():
        kinds.setdefault(actor, set()).add(kind)
    for actor, seen in sorted(kinds.items()):
        if len(seen) > 1:
            problems.append(f"actor {actor!r} classified as both {sorted(seen)}")

    # ---- content class of every identifier-ish column --------------------
    shape_checks = [
        ("events", "event_key", "VARCHAR", "NOT regexp_matches(v, ?)", [EVENT_KEY_RE]),
        ("events", "actor", "VARCHAR", "NOT regexp_matches(v, ?)", [ACTOR_RE]),
        ("events", "content_hash", "VARCHAR", "NOT regexp_matches(v, ?)", [CONTENT_HASH_RE]),
        ("events", "repo", "VARCHAR", "NOT regexp_matches(v, ?)", [REPO_RE]),
        ("objects", "repo", "VARCHAR", "NOT regexp_matches(v, ?)", [REPO_RE]),
        ("objects", "actor", "VARCHAR", "v <> '' AND NOT regexp_matches(v, ?)", [ACTOR_RE]),
        ("objects", "head_commit", "VARCHAR", "v <> '' AND NOT regexp_matches(v, ?)", [HEAD_COMMIT_RE]),
        ("ingest_runs", "run_id", "VARCHAR", "NOT regexp_matches(v, ?)", [RUN_ID_RE]),
        (
            "ingest_runs", "source", "VARCHAR",
            f"v NOT IN ({','.join('?' * len(SOURCES))})", list(SOURCES),
        ),
    ]
    for table, column, dtype, predicate, params in shape_checks:
        bad = _scan(con, table, column, dtype, predicate, params)
        if bad:
            problems.append(f"{table}.{column}: {bad} value(s) outside the expected shape")

    versions = sorted(taxonomy.CLASSIFIER_VERSIONS)
    bad = _scan(
        con, "events", "classifier_version", "VARCHAR",
        f"v NOT IN ({','.join('?' * len(versions))})", versions,
    )
    if bad:
        problems.append(f"events.classifier_version: {bad} value(s) outside {versions}")

    message_classes = sorted(taxonomy.MESSAGE_CLASS)
    bad = _scan(
        con, "objects", "message_class", "VARCHAR",
        f"v NOT IN ({','.join('?' * len(message_classes))})", message_classes,
    )
    if bad:
        problems.append(f"objects.message_class: {bad} value(s) outside the closed set")

    # Ticket keys: shape **and** the configured scope (BUG-04's gate is
    # re-asserted here, so a loosened config cannot be blessed by --verify).
    # prd/18: an explicit key set replaces the prefix check.
    bad = _scan(con, "objects", "ticket_keys", "VARCHAR[]", "NOT regexp_matches(v, ?)", [key_shape])
    if bad:
        problems.append(f"objects.ticket_keys: {bad} value(s) do not match key_shape")
    if key_set:
        bad = _scan(
            con, "objects", "ticket_keys", "VARCHAR[]",
            f"upper(v) NOT IN ({','.join('?' * len(key_set))})", key_set,
        )
        if bad:
            problems.append(
                f"objects.ticket_keys: {bad} value(s) outside the configured"
                f" ticket keys {key_set}"
            )
    else:
        projects = sorted(str(p).upper() for p in ticket_projects)
        prefix_predicate = (
            f"split_part(v, '-', 1) NOT IN ({','.join('?' * len(projects))})" if projects else "TRUE"
        )
        bad = _scan(con, "objects", "ticket_keys", "VARCHAR[]", prefix_predicate, projects)
        if bad:
            problems.append(
                f"objects.ticket_keys: {bad} value(s) outside the configured projects {projects}"
            )
    bad = con.execute(
        "SELECT count(*) FROM objects WHERE object_type = 'PR'"
        " AND (ticket_keys IS NULL OR len(ticket_keys) = 0)"
    ).fetchone()[0]
    if bad:
        problems.append(f"objects: {bad} PR(s) without a matched ticket key")
    # A Jira Story/Epic *is* a ticket: its key must travel on the object, which
    # is what joins the ticket layer to the PR layer (prd/03, prd/04 dev-status).
    for otype in ("STORY", "EPIC"):
        bad = con.execute(
            "SELECT count(*) FROM objects WHERE object_type = ?"
            " AND (ticket_keys IS NULL OR len(ticket_keys) = 0)",
            [otype],
        ).fetchone()[0]
        if bad:
            problems.append(f"objects: {bad} {otype}(s) without their ticket key")

    # Object ids must carry their type and a well-formed suffix.
    for otype, oid, state in con.execute(
        "SELECT object_type, object_id, state FROM objects"
    ).fetchall():
        problems.extend(object_shape_problems(otype, oid, state, key_shape, key_set))

    # ---- referential integrity ------------------------------------------
    orphans = con.execute(
        "SELECT count(*) FROM event_objects eo"
        " LEFT JOIN events e ON eo.event_key = e.event_key WHERE e.event_key IS NULL"
    ).fetchone()[0]
    if orphans:
        problems.append(f"event_objects: {orphans} row(s) reference a missing event")
    dangling = con.execute(
        "SELECT count(*) FROM event_objects eo"
        " LEFT JOIN objects o ON eo.object_id = o.object_id AND eo.object_type = o.object_type"
        " WHERE o.object_id IS NULL"
    ).fetchone()[0]
    if dangling:
        problems.append(f"event_objects: {dangling} row(s) reference a missing object")
    lonely = con.execute(
        "SELECT count(*) FROM events e"
        " WHERE NOT EXISTS (SELECT 1 FROM event_objects eo WHERE eo.event_key = e.event_key)"
    ).fetchone()[0]
    if lonely:
        problems.append(f"events: {lonely} event(s) reference no object")
    unpaired = con.execute(
        "SELECT count(*) FROM events e WHERE e.repo IS NOT NULL AND e.pr_id IS NOT NULL"
        " AND NOT EXISTS ("
        " SELECT 1 FROM objects o WHERE o.object_type = 'PR'"
        " AND o.repo = e.repo AND o.pr_id = e.pr_id)"
    ).fetchone()[0]
    if unpaired:
        problems.append(f"events: {unpaired} event(s) have no matching PR object (repo/pr_id)")

    return {
        "ok": not problems,
        "problems": problems,
        "tables": {t: sorted(ALLOWED.get(t, set())) for t in sorted(tables)},
        "summary": summary(con),
    }

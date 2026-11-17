"""Filtering, pagination and aggregation for the read API.

``ingest.store.list_events`` is the canonical reader for the *whole* log; the
API needs the same rows shape but with server-side filters, an ``offset`` and a
total count, plus a cheap histogram so a client can size a time window without
downloading it. This module is that query layer.

Two rules keep it honest:

* the row shape is byte-for-byte the shape of ``ingest.store.list_events``
  (same keys, same ``Z`` timestamps), so the UI cannot tell the two apart;
* every filter is a bound parameter — a query parameter is never interpolated
  into SQL, and an unknown one is rejected by the endpoint (see ``app.py``)
  rather than silently ignored.

Timestamps stay naive UTC in SQL, as the store writes them; only the boundary
handling for a date-only upper bound lives here (it is inclusive of that day).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from ingest import taxonomy
from ingest import store as pstore

# Everything /api/events accepts. Unknown names are a 400 at the endpoint.
EVENT_PARAMS: tuple[str, ...] = (
    "from", "to", "repo", "actor", "ticket", "activity", "pr_id",
    "limit", "offset", "order",
)
OBJECT_PARAMS: tuple[str, ...] = (
    "type", "repo", "pr_id", "actor", "ticket", "limit", "offset",
)
COUNT_PARAMS: tuple[str, ...] = (
    "from", "to", "repo", "actor", "ticket", "activity", "pr_id", "granularity",
)

GRANULARITIES = {"hour": "hour", "day": "day", "week": "week"}
_BUCKET_MS = {"hour": 3_600_000, "day": 86_400_000, "week": 604_800_000}

DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- #
# time range (BUG-09: a date-only upper bound includes that whole day)
# --------------------------------------------------------------------------- #
def parse_range(from_: str | None, to: str | None) -> tuple[datetime | None, datetime | None]:
    """Turn the ``from``/``to`` query strings into naive-UTC bounds.

    * ``from`` date-only is midnight at the start of that day;
    * ``to`` date-only is *inclusive of the whole day* (the last representable
      naive timestamp of it), because a UI that passes calendar days expects
      the final day to be included;
    * an ISO datetime (``…T23:59:59`` or with an offset) is used exactly.

    Raises ``ValueError`` for unparseable input (the endpoint maps it to 400).
    """
    start = pstore.to_naive_utc(from_) if from_ else None
    if not to:
        return start, None
    if DATE_ONLY.match(to.strip()):
        end = pstore.to_naive_utc(to) + timedelta(days=1) - timedelta(microseconds=1)
    else:
        end = pstore.to_naive_utc(to)
    return start, end


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def _ticket_key(value: str) -> str:
    """Accept ``PROJ-101``, ``TICKET:PROJ-101`` or ``STORY:PROJ-101``."""
    text = str(value or "").strip()
    for prefix in ("TICKET:", "STORY:"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _event_where(
    start: datetime | None = None,
    end: datetime | None = None,
    repo: str | None = None,
    actor: str | None = None,
    activity: str | None = None,
    pr_id: int | None = None,
    ticket: str | None = None,
) -> tuple[str, list]:
    """SQL WHERE clause + bound params for the event log (alias ``e``)."""
    clauses: list[str] = []
    params: list = []
    if start is not None:
        clauses.append("e.ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("e.ts <= ?")
        params.append(end)
    if repo:
        clauses.append("e.repo = ?")
        params.append(repo)
    if actor:
        clauses.append("e.actor = ?")
        params.append(actor)
    if activity:
        clauses.append("e.activity = ?")
        params.append(activity)
    if pr_id is not None:
        clauses.append("e.pr_id = ?")
        params.append(int(pr_id))
    if ticket:
        # prd/25: a ticket filter must match the story's own events too, not
        # only the PR-side TICKET links — otherwise the ticket detail page can
        # show a status change the event log cannot filter to.
        key = _ticket_key(ticket)
        clauses.append(
            "EXISTS (SELECT 1 FROM event_objects eo WHERE eo.event_key = e.event_key"
            " AND eo.object_type IN ('TICKET', 'STORY')"
            " AND eo.object_id IN (?, ?))"
        )
        params += [f"TICKET:{key}", f"STORY:{key}"]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _relations(con, keys: list[str]) -> dict[str, list[str]]:
    rel: dict[str, list[str]] = {key: [] for key in keys}
    if not keys:
        return rel
    for start in range(0, len(keys), 500):
        chunk = keys[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        for key, _otype, oid in con.execute(
            "SELECT event_key, object_type, object_id FROM event_objects"
            f" WHERE event_key IN ({placeholders}) ORDER BY object_type, object_id",
            chunk,
        ).fetchall():
            rel[key].append(oid)
    return rel


def list_events(
    con,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
    order: str = "asc",
    **filters,
) -> tuple[list[dict], int]:
    """A page of the event log plus the total number of matching rows."""
    where, params = _event_where(start, end, **filters)
    total = int(con.execute(f"SELECT count(*) FROM events e{where}", params).fetchone()[0])
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    sql = (
        "SELECT e.event_key, e.ts, e.activity, e.actor, e.repo, e.pr_id,"
        f" e.attributes, e.as_of FROM events e{where}"
        f" ORDER BY e.ts {direction}, e.event_key {direction}"
    )
    page_params = list(params)
    if limit:
        sql += " LIMIT ? OFFSET ?"
        page_params += [int(limit), max(0, int(offset))]
    elif offset:
        sql += " LIMIT 1000000000 OFFSET ?"
        page_params += [max(0, int(offset))]
    rows = con.execute(sql, page_params).fetchall()
    rel = _relations(con, [r[0] for r in rows])
    out = []
    for key, ts, activity, actor, repo, pr_id, attributes, as_of in rows:
        out.append(
            {
                "key": key,
                "ts": pstore.iso(ts),
                "activity": activity,
                "actor": actor,
                "objects": rel.get(key, []),
                "attributes": json.loads(attributes) if isinstance(attributes, str) else (attributes or {}),
                "repo": repo,
                "pr_id": pr_id,
                "as_of": pstore.iso(as_of) if as_of else None,
            }
        )
    return out, total


def list_objects(
    con,
    object_type: str | None = None,
    repo: str | None = None,
    pr_id: int | None = None,
    actor: str | None = None,
    ticket: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """A page of the object table plus the total number of matching rows.

    Reads the stable columns directly (rather than through
    ``ingest.store.list_objects``) so an ingest-side column addition cannot
    change the shape the UI is promised.
    """
    where: list[str] = []
    params: list = []
    if object_type:
        where.append("object_type = ?")
        params.append(object_type)
    if repo:
        where.append("repo = ?")
        params.append(repo)
    if pr_id is not None:
        where.append("pr_id = ?")
        params.append(int(pr_id))
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if ticket:
        key = _ticket_key(ticket)
        # prd/25: the story object carries the same key as the PR links, so a
        # ticket filter must return both or the detail page would look empty.
        where.append("(object_id IN (?, ?) OR list_contains(ticket_keys, ?))")
        params += [f"TICKET:{key}", f"STORY:{key}", key]
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = int(con.execute(f"SELECT count(*) FROM objects{clause}", params).fetchone()[0])

    columns = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'objects'"
        ).fetchall()
    }
    extra = ", CAST(attributes AS VARCHAR) AS attributes" if "attributes" in columns else ", NULL AS attributes"
    sql = (
        "SELECT object_type, object_id, repo, pr_id, ticket_keys, actor, state,"
        f" created_at, closed_at, head_commit, message_class{extra} FROM objects{clause}"
        " ORDER BY object_type, object_id"
    )
    page_params = list(params)
    if limit:
        sql += " LIMIT ? OFFSET ?"
        page_params += [int(limit), max(0, int(offset))]
    elif offset:
        sql += " LIMIT 1000000000 OFFSET ?"
        page_params += [max(0, int(offset))]

    out = []
    for row in con.execute(sql, page_params).fetchall():
        (otype, oid, orepo, opr, keys, oactor, state, created, closed, head, mclass, attrs) = row
        record = {
            "object_type": otype,
            "object_id": oid,
            "repo": orepo,
            "pr_id": opr,
            "ticket_keys": list(keys or []),
            "actor": oactor,
            "state": state,
            "created_at": pstore.iso(created) if created else None,
            "closed_at": pstore.iso(closed) if closed else None,
            "head_commit": head,
            "message_class": mclass,
        }
        if attrs:
            try:
                record["attributes"] = json.loads(attrs) if isinstance(attrs, str) else attrs
            except json.JSONDecodeError:
                record["attributes"] = {}
        out.append(record)
    return out, total


def event_counts(
    con,
    start: datetime | None = None,
    end: datetime | None = None,
    granularity: str = "day",
    **filters,
) -> dict:
    """Histogram of the *matching* log, computed in DuckDB (GAP-04).

    ``buckets`` is dense between the first and last non-empty bucket so a client
    can draw it without filling gaps; each bucket is UTC-aligned (hour/day) or
    Monday-aligned (week). ``by_activity`` and ``by_repo`` are window totals,
    which is what a legend or a swimlane filter list needs — no per-event rows.
    """
    unit = GRANULARITIES.get(str(granularity).lower())
    if unit is None:
        raise ValueError(f"granularity must be one of {sorted(GRANULARITIES)}")
    where, params = _event_where(start, end, **filters)
    rows = con.execute(
        f"SELECT date_trunc('{unit}', e.ts) AS bucket, e.activity, count(*) AS n"
        f" FROM events e{where} GROUP BY 1, 2 ORDER BY 1",
        params,
    ).fetchall()

    counts: dict[datetime, int] = {}
    by_activity: dict[str, int] = {}
    by_repo: dict[str, int] = {}
    for bucket, activity, n in rows:
        counts[bucket] = counts.get(bucket, 0) + int(n)
        by_activity[activity] = by_activity.get(activity, 0) + int(n)
    if rows:
        for repo, n in con.execute(
            f"SELECT e.repo, count(*) FROM events e{where} GROUP BY 1 ORDER BY 2 DESC", params
        ).fetchall():
            by_repo[repo] = int(n)

    buckets: list[dict] = []
    if counts:
        step = timedelta(milliseconds=_BUCKET_MS[unit])
        cursor = min(counts)
        last = max(counts)
        while cursor <= last:
            buckets.append({"t": pstore.iso(cursor), "count": counts.get(cursor, 0)})
            cursor += step

    first = con.execute(f"SELECT min(e.ts), max(e.ts) FROM events e{where}", params).fetchone()
    return {
        "granularity": unit,
        "bucket_ms": _BUCKET_MS[unit],
        "total": sum(counts.values()),
        "buckets": buckets,
        "by_activity": dict(sorted(by_activity.items())),
        "by_repo": by_repo,
        "activity_vocabulary": sorted(taxonomy.ACTIVITIES),
        "first_ts": pstore.iso(first[0]) if first and first[0] else None,
        "last_ts": pstore.iso(first[1]) if first and first[1] else None,
    }

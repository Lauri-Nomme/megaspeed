"""Team annotations (prd/61): dated changes the team itself records.

A trend answers "is it getting better?" only if the reader knows *what changed*.
A policy change is first-party team metadata — the team types it into megaspeed
("we adopted the 4 h review SLA after the retro") — so it lives in the store,
in its own ``annotations`` table, next to the events it explains. It is **not**
an event: it never reaches the raw→label classifier, the event log or an LLM
view (prd/02 "Scope"), and it is never scraped.

The ``kind`` is a closed vocabulary (`ingest.taxonomy.ANNOTATION_KINDS`) so the
chart marker's label is renderable and testable: ``review_sla`` + ``4`` renders
"review SLA 4 h". ``note`` is the one documented first-party free-text column
(the team's own words, stored verbatim); every other column keeps the store's
usual identifier shape.

Calendar spans (holidays and vacations) are **derived, never entered twice**:
they come from the same business-hours calendar every duration is priced on
(prd/19). An annotation outside the analysis window is counted, never drawn.

One store, one transactional write, one cache invalidation: a write bumps the
store stamp, so the server's trend cache invalidates exactly like an ingest run.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import date, datetime

from ingest.store import REPO_RE, iso, utcnow

#: The closed vocabulary, with its display label and the unit of its optional
#: numeric value. ``numeric`` is false for a change that has no number ("sentinel
#: added"); a value supplied for it is still accepted (some teams note the count
#: of sentinels) and rendered after the label.
KINDS: dict[str, dict] = {
    "review_sla": {"label": "review SLA", "unit": "h", "numeric": True},
    "wip_limit": {"label": "WIP limit", "unit": None, "numeric": True},
    "size_cap": {"label": "PR size cap", "unit": "commits", "numeric": True},
    "sentinel_added": {"label": "sentinel added", "unit": None, "numeric": False},
    "sentinel_rotation": {"label": "sentinel rotation", "unit": None, "numeric": False},
    "refinement_change": {"label": "refinement change", "unit": None, "numeric": False},
    "other": {"label": "other", "unit": None, "numeric": False},
}
#: Display order of the catalogue (stable, independent of dict order).
ORDER = (
    "review_sla", "wip_limit", "size_cap", "sentinel_added",
    "sentinel_rotation", "refinement_change", "other",
)
#: A note is first-party prose, not an unbounded field: the cap keeps one row
#: from becoming a document. The text itself is stored verbatim.
NOTE_MAX = 2000
#: Kinds that represent a policy/process change (what "Did it work?" compares).
POLICY_KINDS = frozenset({"review_sla", "wip_limit", "size_cap",
                          "sentinel_added", "sentinel_rotation",
                          "refinement_change"})

_REPO_RE = re.compile(REPO_RE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AnnotationError(ValueError):
    """A malformed or unknown annotation. The API maps it to a 400."""


def catalogue() -> list[dict]:
    """The closed vocabulary the UI builds its menu from (one place)."""
    return [
        {"id": kind, "label": KINDS[kind]["label"], "unit": KINDS[kind]["unit"],
         "numeric": KINDS[kind]["numeric"]}
        for kind in ORDER
    ]


def kinds() -> tuple[str, ...]:
    return tuple(ORDER)


def _number(value, *, where: str = "value") -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise AnnotationError(f"{where} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AnnotationError(f"{where} must be a number") from None
    if not math.isfinite(number):
        raise AnnotationError(f"{where} must be a finite number")
    return number


def _date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not _DATE_RE.match(text):
        raise AnnotationError("date must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise AnnotationError(f"{text!r} is not a calendar date") from None


def _note(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnnotationError("note must be text")
    if "\x00" in value:
        raise AnnotationError("note must not contain NUL")
    if len(value) > NOTE_MAX:
        raise AnnotationError(f"note is longer than {NOTE_MAX} characters")
    return value


def validate(entry: dict) -> dict:
    """Normalise one annotation; raise :class:`AnnotationError` when invalid.

    Only the ``kind`` is a closed-vocabulary decision (an unknown kind is the one
    error the contract calls out explicitly); a bad date/value/scope is a
    malformed request and is rejected the same way rather than silently coerced.
    ``note`` and ``scope`` are optional; the note is stored verbatim.
    """
    if not isinstance(entry, dict):
        raise AnnotationError("an annotation must be an object")
    kind = str(entry.get("kind") or "").strip()
    if kind not in KINDS:
        raise AnnotationError(
            f"unknown annotation kind {kind!r}: kind must be one of {', '.join(ORDER)}"
        )
    value = _number(entry.get("value"))
    scope = (str(entry.get("scope")).strip() or None) if entry.get("scope") else None
    if scope is not None and not _REPO_RE.match(scope):
        raise AnnotationError(
            "scope must be a repo (project/slug), e.g. DEL/delivery-web"
        )
    return {
        "date": _date(entry.get("date")).isoformat(),
        "kind": kind,
        "value": value,
        "note": _note(entry.get("note")),
        "scope": scope,
    }


def render(entry: dict) -> str:
    """The closed label for one entry (``review SLA 4 h`` · ``WIP limit 12``)."""
    kind = entry.get("kind")
    spec = KINDS.get(kind)
    if spec is None:
        return str(kind or "annotation")
    label = spec["label"]
    value = entry.get("value")
    if value is None:
        return label
    number = float(value)
    shown = f"{number:.10g}"
    unit = spec["unit"]
    return f"{label} {shown}{f' {unit}' if unit else ''}"


def _row(row) -> dict:
    (annotation_id, created_at, when, kind, value, note, scope) = row
    entry = {
        "id": annotation_id,
        "created_at": iso(created_at),
        "date": when.isoformat() if hasattr(when, "isoformat") else str(when),
        "kind": kind,
        "value": float(value) if value is not None else None,
        "note": note,
        "scope": scope,
    }
    entry["label"] = render(entry)
    entry["policy"] = kind in POLICY_KINDS
    return entry


def load(con, repo: str | None = None) -> list[dict]:
    """Every annotation, oldest change first (optionally one repo's scope).

    A store with no ``annotations`` table (an older build) reads as empty: an
    annotation is additive metadata, never a reason to fail a read.
    """
    try:
        rows = con.execute(
            "SELECT annotation_id, created_at, date, kind, value, note, scope"
            " FROM annotations ORDER BY date ASC, created_at ASC, annotation_id ASC"
        ).fetchall()
    except Exception:  # noqa: BLE001 — table absent on a store from an older build
        return []
    entries = [_row(row) for row in rows]
    if repo:
        entries = [
            entry for entry in entries
            if entry["scope"] in (None, repo)
        ]
    return entries


def save(con, entry: dict) -> dict:
    """Insert one validated annotation and return the stored row.

    The caller owns the transaction (the server wraps it): a failure leaves no
    partial row.
    """
    clean = validate(entry)
    annotation_id = uuid.uuid4().hex
    created_at = utcnow()
    con.execute(
        "INSERT INTO annotations"
        " (annotation_id, created_at, date, kind, value, note, scope)"
        " VALUES (?, ?, CAST(? AS DATE), ?, ?, ?, ?)",
        [annotation_id, created_at, clean["date"], clean["kind"], clean["value"],
         clean["note"], clean["scope"]],
    )
    return _row((annotation_id, created_at, date.fromisoformat(clean["date"]),
                 clean["kind"], clean["value"], clean["note"], clean["scope"]))


def delete(con, annotation_id: str) -> bool:
    """Delete one annotation; ``False`` when the id is not in the store."""
    existing = int(con.execute(
        "SELECT count(*) FROM annotations WHERE annotation_id = ?", [annotation_id]
    ).fetchone()[0])
    if not existing:
        return False
    con.execute("DELETE FROM annotations WHERE annotation_id = ?", [annotation_id])
    return True


def _day(value: date) -> date:
    return value.date() if isinstance(value, datetime) else value


def in_window(entry: dict, window: tuple[datetime, datetime] | None) -> bool:
    """Is the annotation's date inside the analysis window (inclusive)?"""
    if window is None:
        return True
    start, end = window
    when = (date.fromisoformat(entry["date"])
            if isinstance(entry["date"], str) else entry["date"])
    return _day(start) <= when <= _day(end)


def view_entries(entries: list[dict],
                 window: tuple[datetime, datetime] | None = None) -> list[dict]:
    """Annotate each entry with ``in_window``; the window's counts travel too."""
    out = []
    for entry in entries:
        row = dict(entry)
        row["in_window"] = in_window(entry, window)
        out.append(row)
    inside = [row for row in out if row["in_window"]]
    return out, {
        "policy": sum(1 for row in inside if row["kind"] in POLICY_KINDS),
        "holiday": 0,
        "vacation": 0,
        "inside": len(inside),
        "outside": len(out) - len(inside),
        "total": len(out),
    }


def calendar_spans(court, window: tuple[datetime, datetime] | None = None) -> list[dict]:
    """Holiday/vacation spans from the business-hours calendar (never typed in).

    Dated holidays are one-offs; recurring holidays are month/day rules, so they
    are expanded for each year the window covers (a span for every year ever
    would be infinite). Vacations carry their person (a pseudonym) and range.
    """
    calendar = getattr(court, "calendar", None)
    if calendar is None:
        return []
    snapshot = getattr(court, "snapshot", None) or utcnow()
    years = (
        range(_day(window[0]).year, _day(window[1]).year + 1)
        if window is not None else (snapshot.year,)
    )
    spans: list[dict] = []
    summary = calendar.summary()
    for holiday in summary.get("holidays", {}).get("dated", ()):
        spans.append({
            "kind": "holiday",
            "start": holiday["date"],
            "end": holiday["date"],
            "label": holiday.get("name") or "public holiday",
        })
    for holiday in summary.get("holidays", {}).get("recurring", ()):
        try:
            month, day = (int(part) for part in str(holiday["month_day"]).split("-"))
        except (KeyError, TypeError, ValueError):
            continue
        for year in years:
            try:
                when = date(year, month, day)
            except ValueError:  # 29 Feb outside a leap year
                continue
            spans.append({
                "kind": "holiday",
                "start": when.isoformat(),
                "end": when.isoformat(),
                "label": holiday.get("name") or "public holiday",
            })
    # A dated entry wins over the recurring rule for the same day (a tenant can
    # override a national holiday's kind), so the same day is never two bands.
    unique: dict[tuple[str, str], dict] = {}
    for span in spans:
        unique.setdefault((span["start"], span["end"]), span)
    spans = list(unique.values())
    for vacation in calendar.vacation_list():
        spans.append({
            "kind": "vacation",
            "start": vacation["start"],
            "end": vacation["end"],
            "label": f"{vacation.get('person') or 'team'} on vacation",
            "person": vacation.get("person"),
        })
    spans.sort(key=lambda span: (span["start"], span["kind"], span["label"]))
    for span in spans:
        span["in_window"] = _span_in_window(span, window)
    return spans


def _span_in_window(span: dict, window: tuple[datetime, datetime] | None) -> bool:
    if window is None:
        return True
    start, end = window
    low, high = date.fromisoformat(span["start"]), date.fromisoformat(span["end"])
    return low <= _day(end) and high >= _day(start)

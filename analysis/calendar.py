"""Business-hours calendar (prd/19): working hours, holidays, vacations.

Wall-clock waits overstate latency — nights and weekends dominate a review
queue — so every "hours waited" number is converted to **business hours** with
a per-person calendar:

    config/calendar.default.yaml      committed: one timezone, the default
                                      working window, Estonian public holidays
                                      (24 Dec and 31 Dec are half-days)
    data/<tenant>/calendar.yaml       user-editable: per-person `overrides`,
                                      extra `holidays`, per-person `vacations`

The tenant file *extends* the default (same key wins, vacations accumulate), so
a tenant never has to restate the national holidays.

The computation is one function:

    business_hours(person, t1, t2)
        = sum over local days in [t1, t2) of the person's working window,
          minus their vacations, minus holidays (a half-day keeps the first
          half of the window)

`person` is a pseudonym (or ``None`` for an unattributed/queue wait, which uses
the default window). No calendar configured at all means the caller falls back
to wall-clock hours — the old behaviour, labelled as such.

Out of scope (prd/19): per-person timezones (one ``timezone`` for now) and
non-Estonian holidays (add them in the tenant file).

Read-only, like the rest of ``analysis/``: it reads YAML config files, never the
store, and never a raw name — a person is a pseudonym in both places.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:  # PyYAML is the only dependency; a missing one is a clear error, not a crash
    import yaml
except ImportError:  # pragma: no cover - the venv installs it (requirements.txt)
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "calendar.default.yaml"
TENANT_FILE = "calendar.yaml"

FULL = "full"
HALF = "half"
HOLIDAY_KINDS = (FULL, HALF)

#: Upper bound on the per-calendar ``business_hours`` memo. The real payloads
#: ask ~1.6k unique questions at 4k stories; the cap only exists so a client
#: that sweeps arbitrary windows cannot grow one dict without limit.
HOURS_CACHE_SIZE = 200_000

# "09:00–17:00", "09:00-17:00", "9-17", "12:00 — 20:00" (the PRD writes an en dash)
_WINDOW_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*$"
)
_OFF = {"off", "none", "closed", "false", "no"}


class CalendarError(ValueError):
    """A calendar config file is malformed; the message names the file and key."""


# --------------------------------------------------------------------------- #
# parsing primitives
# --------------------------------------------------------------------------- #
def _minutes(hour: int, minute: int, where: str) -> int:
    if not (0 <= hour <= 24) or not (0 <= minute <= 59):
        raise CalendarError(f"{where}: {hour:02d}:{minute:02d} is not a time of day")
    value = hour * 60 + minute
    if value > 24 * 60:
        raise CalendarError(f"{where}: the time is past midnight")
    return value


def parse_window(value, where: str = "working_hours") -> tuple[int, int] | None:
    """``"09:00–17:00"`` -> ``(540, 1020)`` minutes; ``"off"`` -> ``None``."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        start = _minutes(int(value[0]), 0, where)
        end = _minutes(int(value[1]), 0, where)
    elif isinstance(value, str):
        text = value.strip()
        if text.lower() in _OFF:
            return None
        match = _WINDOW_RE.match(text)
        if not match:
            raise CalendarError(
                f"{where}: {value!r} is not a working window like '09:00–17:00'"
            )
        start = _minutes(int(match.group(1)), int(match.group(2) or 0), where)
        end = _minutes(int(match.group(3)), int(match.group(4) or 0), where)
    else:
        raise CalendarError(f"{where}: {value!r} is not a working window")
    if end <= start:
        raise CalendarError(
            f"{where}: {value!r} ends before it starts (cross-midnight shifts are out of scope)"
        )
    return start, end


def _parse_date(value, where: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            raise CalendarError(f"{where}: {value!r} is not an ISO date (YYYY-MM-DD)") from None
    raise CalendarError(f"{where}: {value!r} is not a date")


def _parse_month_day(value, where: str) -> tuple[int, int]:
    text = str(value).strip()
    match = re.match(r"^(\d{1,2})-(\d{1,2})$", text)
    if not match:
        raise CalendarError(f"{where}: {value!r} is not a month-day like '12-24'")
    month, day = int(match.group(1)), int(match.group(2))
    try:
        date(2000, month, day)  # leap year: 02-29 is legal
    except ValueError:
        raise CalendarError(f"{where}: {value!r} is not a calendar day") from None
    return month, day


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm — Easter is the one moving holiday we need."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _fmt_window(window: tuple[int, int] | None) -> str:
    if window is None:
        return "off"
    return f"{window[0] // 60:02d}:{window[0] % 60:02d}-{window[1] // 60:02d}:{window[1] % 60:02d}"


@dataclass(frozen=True)
class _Holiday:
    kind: str
    name: str | None = None


# --------------------------------------------------------------------------- #
# the calendar
# --------------------------------------------------------------------------- #
@dataclass
class Calendar:
    """One timezone, a default window, per-person overrides, holidays, vacations."""

    timezone: str
    default_window: tuple[int, int] | None
    weekdays: frozenset[int]
    overrides: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    dated_holidays: dict[date, _Holiday] = field(default_factory=dict)
    recurring_holidays: dict[tuple[int, int], _Holiday] = field(default_factory=dict)
    vacations: dict[str, list[tuple[date, date]]] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    _tz: ZoneInfo = field(default=None, repr=False, compare=False)  # type: ignore[assignment]
    _cache: dict = field(default_factory=dict, repr=False, compare=False)
    # ``business_hours`` is asked the same question dozens of times per payload
    # (93,400 calls for 1,615 unique arguments at 5,240 PRs), and it is a pure
    # function of (person, t1, t2, ignore_vacations), so one dict memo per
    # calendar removes the repeat. The calendar itself is cached per calendar
    # file version by the server (``_calendar_for``), so an edit to a calendar
    # config drops the memo with it — a store reload does not, and need not:
    # the answer depends on the config, never on the store.
    _hours_cache: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._tz is None:
            try:
                self._tz = ZoneInfo(self.timezone)
            except Exception:  # noqa: BLE001 - unknown tz is a config error
                raise CalendarError(
                    f"timezone {self.timezone!r} is not a known IANA timezone"
                ) from None

    # -- queries ------------------------------------------------------------ #
    @property
    def hours_mode(self) -> str:
        return "business"

    def window_for(self, person: str | None) -> tuple[int, int] | None:
        """The person's working window: their override, else the default."""
        if person is not None and person in self.overrides:
            return self.overrides[person]
        return self.default_window

    def holiday_for(self, day: date) -> _Holiday | None:
        hit = self.dated_holidays.get(day)
        if hit is not None:
            return hit
        return self.recurring_holidays.get((day.month, day.day))

    def on_vacation(self, person: str | None, day: date) -> bool:
        if person is None:
            return False
        return any(start <= day <= end for start, end in self.vacations.get(person, ()))

    def working_days(self, person: str | None, start: date, end: date,
                     ignore_vacations: bool = False) -> int:
        """Working days in the inclusive date range (for the vacation signal)."""
        total = 0
        day = start
        while day <= end:
            if self._window_for_day(person, day, ignore_vacations) is not None:
                total += 1
            day += timedelta(days=1)
        return total

    def day_bounds(self, start: date, end: date) -> tuple[datetime, datetime]:
        """``[start 00:00, end+1 00:00)`` local time, as naive UTC (store convention)."""
        begin = self._at(start, 0)
        finish = self._at(end + timedelta(days=1), 0)
        return (
            begin.astimezone(timezone.utc).replace(tzinfo=None),
            finish.astimezone(timezone.utc).replace(tzinfo=None),
        )

    def business_hours(self, person: str | None, t1: datetime, t2: datetime,
                       ignore_vacations: bool = False) -> float:
        """The person's working hours inside ``[t1, t2)``, in hours.

        ``t1``/``t2`` are the naive-UTC timestamps the store holds; they are
        converted to the calendar's timezone before the day walk. Vacations and
        full holidays contribute nothing, a half-day keeps the first half of the
        window, weekends are excluded by ``weekdays``.

        ``ignore_vacations`` prices the time the person *would* have worked had
        they not been away — what the vacation-blocking signal needs, since a
        vacation's own business hours are zero by definition.

        The answer is memoised per calendar (see ``_hours_cache``): the metrics
        ask for the same (person, t1, t2) many times over, and the day walk is
        the expensive part. The result is a pure function of the arguments and
        the config, so the memo cannot go stale while this calendar is the one
        the server cached for the tenant's calendar version.
        """
        if t1 is None or t2 is None or t2 <= t1:
            return 0.0
        key = (person, t1, t2, ignore_vacations)
        hit = self._hours_cache.get(key)
        if hit is not None:
            return hit
        start = self._local(t1)
        end = self._local(t2)
        seconds = 0.0
        day = start.date()
        last = end.date()
        while day <= last:
            window = self._window_for_day(person, day, ignore_vacations)
            if window is not None:
                low = max(window[0], start)
                high = min(window[1], end)
                if high > low:
                    seconds += (high - low).total_seconds()
            day += timedelta(days=1)
        value = seconds / 3600.0
        # Bounded like the server's other caches: a pathological client cannot
        # grow the memo without limit, and a clear only costs one recompute.
        if len(self._hours_cache) >= HOURS_CACHE_SIZE:
            self._hours_cache.clear()
        self._hours_cache[key] = value
        return value

    def describe(self) -> str:
        """One sentence for the API ``assumption`` field."""
        if self.default_window is None:
            hours = "no working window (24/7)"
        else:
            hours = f"Mon–Fri {_fmt_window(self.default_window)}" if self.weekdays == frozenset(
                range(1, 6)
            ) else f"{_fmt_window(self.default_window)}"
        return (
            f"business hours ({hours} {self.timezone}; holidays and vacations excluded)"
        )

    def summary(self) -> dict:
        """The effective calendar, for ``GET /api/calendar``."""
        return {
            "hours_mode": self.hours_mode,
            "timezone": self.timezone,
            "weekdays": sorted(self.weekdays),
            "working_hours": {"default": _fmt_window(self.default_window)},
            "overrides": {person: _fmt_window(window) for person, window in sorted(self.overrides.items())},
            "holidays": {
                "dated": [
                    {"date": day.isoformat(), "type": entry.kind, "name": entry.name}
                    for day, entry in sorted(self.dated_holidays.items())
                ],
                "recurring": [
                    {
                        "month_day": f"{month:02d}-{day:02d}",
                        "type": entry.kind,
                        "name": entry.name,
                    }
                    for (month, day), entry in sorted(self.recurring_holidays.items())
                ],
                "count": len(self.dated_holidays) + len(self.recurring_holidays),
            },
            "vacations": self.vacation_list(),
            "sources": list(self.sources),
            "assumption": self.describe(),
        }

    def vacation_list(self) -> list[dict]:
        """Every vacation period, sorted, with its working days."""
        rows = []
        for person, ranges in self.vacations.items():
            for start, end in ranges:
                rows.append({
                    "person": person,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "days": (end - start).days + 1,
                    # working days the vacation covers: the time it costs
                    "business_days": self.working_days(
                        person, start, end, ignore_vacations=True
                    ),
                })
        rows.sort(key=lambda row: (row["start"], row["person"]))
        return rows

    # -- internals ---------------------------------------------------------- #
    def _local(self, stamp: datetime) -> datetime:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(self._tz)

    def _window_for_day(
        self, person: str | None, day: date, ignore_vacations: bool = False
    ) -> tuple[datetime, datetime] | None:
        """The person's working window on ``day`` as aware local datetimes."""
        key = (person, day, ignore_vacations)
        if key in self._cache:
            return self._cache[key]
        result: tuple[datetime, datetime] | None = None
        window = self.window_for(person)
        holiday = self.holiday_for(day)
        if (
            window is not None
            and day.isoweekday() in self.weekdays
            and (ignore_vacations or not self.on_vacation(person, day))
            and (holiday is None or holiday.kind != FULL)
        ):
            start, end = window
            if holiday is not None and holiday.kind == HALF:
                end = start + (end - start) // 2  # a half-day is the morning
            result = (self._at(day, start), self._at(day, end))
        self._cache[key] = result
        return result

    def _at(self, day: date, minutes: int) -> datetime:
        extra, remainder = divmod(minutes, 24 * 60)
        return datetime.combine(
            day + timedelta(days=extra), time(remainder // 60, remainder % 60), tzinfo=self._tz
        )


def business_hours(
    calendar: Calendar | None, person: str | None, t1: datetime, t2: datetime
) -> float:
    """Business hours in ``[t1, t2)``, falling back to wall-clock without a calendar.

    This is the single entry point the analysis uses; ``Calendar.business_hours``
    is the same function once a calendar exists.
    """
    if calendar is None:
        if t1 is None or t2 is None or t2 <= t1:
            return 0.0
        return (t2 - t1).total_seconds() / 3600.0
    return calendar.business_hours(person, t1, t2)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _read(path: Path) -> dict:
    if yaml is None:  # pragma: no cover - requirements.txt installs PyYAML
        raise CalendarError("PyYAML is not installed, so calendar YAML cannot be read")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CalendarError(f"{path.name}: {exc.strerror}") from None
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise CalendarError(f"{path.name}: not valid YAML ({exc})") from None
    if not isinstance(data, dict):
        raise CalendarError(f"{path.name}: the top level must be a mapping")
    return data


def _holidays(raw, where: str) -> tuple[dict[date, _Holiday], dict[tuple[int, int], _Holiday]]:
    dated: dict[date, _Holiday] = {}
    recurring: dict[tuple[int, int], _Holiday] = {}
    if raw is None:
        return dated, recurring
    if not isinstance(raw, list):
        raise CalendarError(f"{where}: holidays must be a list of {{date|month_day, type}}")
    for entry in raw:
        if isinstance(entry, (date, datetime, str)):
            entry = {"date": entry}
        if not isinstance(entry, dict):
            raise CalendarError(f"{where}: {entry!r} is not a holiday entry")
        kind = str(entry.get("type") or FULL).strip().lower()
        if kind not in HOLIDAY_KINDS:
            raise CalendarError(f"{where}: type {kind!r} must be one of {list(HOLIDAY_KINDS)}")
        name = entry.get("name")
        holiday = _Holiday(kind, str(name) if name else None)
        if entry.get("date") is not None:
            dated[_parse_date(entry["date"], where)] = holiday
        elif entry.get("month_day") is not None:
            recurring[_parse_month_day(entry["month_day"], where)] = holiday
        else:
            raise CalendarError(f"{where}: a holiday needs 'date' or 'month_day'")
    return dated, recurring


def _easter_holidays(years, where: str) -> dict[date, _Holiday]:
    """Easter Sunday plus Good Friday (offset −2) for each configured year."""
    out: dict[date, _Holiday] = {}
    if years is None:
        return out
    if not isinstance(years, list):
        raise CalendarError(f"{where}: easter must be a list of years")
    for value in years:
        try:
            year = int(value)
        except (TypeError, ValueError):
            raise CalendarError(f"{where}: {value!r} is not a year") from None
        sunday = easter_sunday(year)
        out[sunday] = _Holiday(FULL, f"ülestõusmispühad {year}")
        out[sunday - timedelta(days=2)] = _Holiday(FULL, f"suur reede {year}")
    return out


def _ranges(raw, where: str) -> list[tuple[date, date]]:
    if raw is None:
        return []
    entries = raw if isinstance(raw, list) else [raw]
    out: list[tuple[date, date]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("start") is None or entry.get("end") is None:
            raise CalendarError(f"{where}: a vacation needs 'start' and 'end' dates")
        start = _parse_date(entry["start"], where)
        end = _parse_date(entry["end"], where)
        if end < start:
            raise CalendarError(f"{where}: {start} → {end} ends before it starts")
        out.append((start, end))
    return out


def _vacations(raw, where: str) -> dict[str, list[tuple[date, date]]]:
    out: dict[str, list[tuple[date, date]]] = {}
    if raw is None:
        return out
    if isinstance(raw, dict):
        for person, ranges in raw.items():
            out.setdefault(str(person), []).extend(_ranges(ranges, f"{where}.{person}"))
        return out
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("person"):
                raise CalendarError(f"{where}: a vacation entry needs a 'person'")
            person = str(entry["person"])
            out.setdefault(person, []).extend(_ranges(entry, f"{where}.{person}"))
        return out
    raise CalendarError(f"{where}: vacations must be a mapping person → ranges")


def _weekdays(raw, where: str) -> frozenset[int]:
    if raw is None:
        return frozenset(range(1, 6))
    if not isinstance(raw, list) or not raw:
        raise CalendarError(f"{where}: weekdays must be a non-empty list (1 = Monday)")
    days = set()
    for value in raw:
        try:
            day = int(value)
        except (TypeError, ValueError):
            raise CalendarError(f"{where}: {value!r} is not a weekday number") from None
        if not 1 <= day <= 7:
            raise CalendarError(f"{where}: weekday {day} is outside 1–7 (ISO, 1 = Monday)")
        days.add(day)
    return frozenset(days)


def _build(base: dict, tenant: dict, sources: list[str]) -> Calendar:
    def pick(key, default=None):
        if key in tenant:
            return tenant[key]
        return base.get(key, default)

    timezone_name = str(pick("timezone") or "UTC")
    weekdays = _weekdays(pick("weekdays"), "weekdays")

    working = dict(base.get("working_hours") or {})
    working.update(tenant.get("working_hours") or {})
    if "default" in working:
        default_window = parse_window(working["default"], "working_hours.default")
    else:
        default_window = (0, 24 * 60)  # no window configured: every day counts

    overrides_raw = {}
    overrides_raw.update(base.get("overrides") or {})
    overrides_raw.update(tenant.get("overrides") or {})
    overrides = {
        str(person): parse_window(value, f"overrides.{person}")
        for person, value in overrides_raw.items()
    }

    dated, recurring = _holidays(base.get("holidays"), "holidays")
    more_dated, more_recurring = _holidays(tenant.get("holidays"), "holidays")
    dated.update(more_dated)
    recurring.update(more_recurring)
    years = list(base.get("easter") or []) + list(tenant.get("easter") or [])
    dated.update(_easter_holidays(years, "easter"))

    vacations = _vacations(base.get("vacations"), "vacations")
    for person, ranges in _vacations(tenant.get("vacations"), "vacations").items():
        vacations.setdefault(person, []).extend(ranges)

    return Calendar(
        timezone=timezone_name,
        default_window=default_window,
        weekdays=weekdays,
        overrides=overrides,
        dated_holidays=dated,
        recurring_holidays=recurring,
        vacations=vacations,
        sources=tuple(sources),
    )


def load(
    tenant: str | None = None,
    tenants_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Calendar | None:
    """Load the default calendar plus the tenant's ``calendar.yaml``.

    Returns ``None`` when no default config exists: the analysis then reports
    wall-clock hours, exactly as before this feature. A malformed file raises
    :class:`CalendarError` (the caller decides whether to surface it), because a
    silently ignored calendar would make every number quietly wrong.
    """
    default_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG
    if not default_path.exists():
        return None
    base = _read(default_path)
    sources = [str(default_path)]
    layer: dict = {}
    if tenant and tenants_dir is not None:
        tenant_path = Path(tenants_dir) / tenant / TENANT_FILE
        if tenant_path.exists():
            layer = _read(tenant_path)
            sources.append(str(tenant_path))
    return _build(base, layer, sources)

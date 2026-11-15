# 19 — Business-hours calendar

Wall-clock waits overstate latency (nights/weekends dominate). Convert to business hours with a
per-person calendar (overrides, vacations, holidays), and surface **vacation-blocking** as a signal.

## Config (main-side, user-editable)

- `config/calendar.default.yaml` (committed): default working hours (e.g. `09:00–17:00`, one tz) +
  **Estonian holidays** (full + half-days, e.g. 24 Dec / 31 Dec half-days).
- `data/<tenant>/calendar.yaml` (per-tenant, user-editable, overrides/extends the default):
  - `overrides` — per-person (pseudonym) working hours, e.g. `LaNo: "12:00–20:00"` (starts noon, finishes late).
  - `holidays` — additions/overrides (`{date, type: full|half}`).
  - `vacations` — per-person `{start, end}` ranges.

## Business-hours computation

For a wait interval attributed to person P: sum P's working hours within the interval, minus P's
vacations and holidays (half-days count half). Applied everywhere the analysis currently reports
"wall-clock hours" (ball-in-court, blocking graph, queue, review latency, insights). `hours_mode`
becomes `business` when a calendar is present, else `wall-clock`.

## Vacation-blocking signal

New insight: for each vacation, "P on vacation D1–D2 → N items waited on P for X business-hours with
no delegation". Surfaces the "someone goes on vacation and things block" pattern.

## Out of scope

Per-person timezones (one tz for now) · non-Estonian holidays (add via config).

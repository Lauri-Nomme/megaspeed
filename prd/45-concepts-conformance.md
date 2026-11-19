# 45 — Concepts conformance: reconcile the implementation against the glossary

`docs/concepts.md` is the single source of truth, but no PRD ever *systematically* audited the
implementation against it — the discrepancies were fixed piecemeal through prd/34–44, each touching only
the spot its feature happened to reach. That leaves lurking drift. This PRD is the missing conformance
pass: enumerate every term, constant and unit in `concepts.md`, grep the codebase, and reconcile.

## Method

1. **Enumerate the glossary** — every term in `concepts.md` (§1–§8) with its canonical spelling, unit and
   constant (`d = 8 h` → `BUSINESS_HOURS_PER_DAY = 8.0`; a week = 40 h → `WAITING_WEEK_HOURS`; the segment
   names `created → ready` etc.; the stage labels; the `push_class`/`thread_outcome`/… closed sets).
2. **Grep for drift** — stale spellings, stale constants, and hardcoded literals that should go through a
   shared constant/formatter. For each hit, decide: conform, or it's a documented exception.
3. **Reconcile** — fix the drift; keep `concepts.md` authoritative (amend it first if the *definition*
   changed, then conform the code).

## Known drift (found so far)

- **`analysis/ballcourt.py:1388`** — `days = span_hours / 24.0` (a `d=24h` leftover); should be
  `span_hours / BUSINESS_HOURS_PER_DAY` (8.0). `per_day` at line 1399 then reports business-days, not
  24-hour units. *(Confirmed: every other duration in the file already divides by 8.0.)*
- To sweep: any other `hours / 24` (outside the wall-clock `fmtCalendarDuration`, which correctly means
  *calendar* days), any stale segment label (`ready → refined`, `backlog`), any stale threshold
  (`72 h` as "3 d"), any legacy `todo` status string in the wrong place, and any closed-set literal that
  isn't routed through `taxonomy`.

## Future-proofing (so this can't regress)

- A **conformance test** (or a small lint check) that asserts:
  - no business-duration code path divides by `24` — business days come from `BUSINESS_HOURS_PER_DAY`;
  - all duration *formatting* goes through the shared `fmtDuration`/`fmtCalendarDuration` (JS) and
    `_human_hours` (Python) — no ad-hoc `hours/24` or `hours/8` string building;
  - the segment/phase/stage labels rendered anywhere match the `concepts.md` spellings.
- A note in `concepts.md` ("How to use this file") that the conformance test is the enforcement.

## Definition of done

- The `/24` in `ballcourt.py:1388` is `/8` (business days), with a regression test pinning the `per_day`
  value.
- The sweep finds and fixes any other drift; the conformance test passes and fails on a reintroduced
  literal.
- `concepts.md` remains authoritative and is amended where the *code* is right and the doc was wrong.

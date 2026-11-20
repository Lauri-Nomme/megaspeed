# 56 — UI papercuts (layout + interaction)

The remaining layout/wording papercuts from `docs/QA-final.md` (group C of the old prd/47). Each is a
small, independent fix.

## Items

1. **Drawer honesty** — the PR drawer shows "waiting since / waiting N d" and its phase list sums to the
   cycle.
2. **Context menu** — "Scope to" offers the *holder* (FrMo), not just the author (ErLi); renders at the
   pointer.
3. **Blocking-map text view** — a node/edge table (not canvas-only) so numbers are readable without
   hover; waits rendered in "d h" like every other view, not "1348h".
4. **Scope banner** — "this view is not filtered" must match what the scope actually does (a `person`
   scope that changes Overview isn't "not filtered").
5. **Copy-as-Markdown** — include the open-PR wait/approval state for a Done-in-Jira ticket.
6. **Wording** — "logs 0.49× less" → a clear phrase; "sentinel waited on ready" → "PRs waited for merge
   after approval"; define "unobserved" inline; define "d" wherever used; don't show timeline lifecycles
   in "calendar days" under a "business hours" header.

## Definition of done
Each item fixed with a node test (or a source guard) where testable; no regression to desktop/phone
layout.

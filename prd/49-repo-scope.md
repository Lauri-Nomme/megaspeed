# 49 — Repo scope dimension

## User journey
A lead asks "is `delivery-api` slower than `delivery-web`?". The `/api/events` and `/api/objects`
endpoints accept `repo`, but Scope, Queue, Blocking, Rules and the Policy lab don't — so the question
can't be asked in the UI. (The data says the two repos are near-identical on cycle/response/sentinel —
the point is the *dimension* exists, not that it finds a gap.)

## Main design
Add `repo` as a first-class scope dimension beside `person`/`role`: the scope control offers the repo
list, and Queue/Blocking/Delivery/Rules/Lab all accept a `repo=` filter (a repo-scoped store slice).
Reuse the existing scope plumbing (`scope.js`, `matchesScope`, the `?scope=` URL param) — `repo` is one
more `{kind, value}`. Rules and the lab recompute against the repo-scoped cohort (the store already has
per-repo objects/events).

## Definition of done
Scoping to `delivery-api` filters Queue/Blocking/Delivery/Rules/Lab to that repo; the URL round-trips
`repo=…`; a repo-scoped rule uses the repo's cohort. Covered by tests (scope plumbing + one
repo-scoped metric).

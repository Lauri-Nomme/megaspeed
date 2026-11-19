# 42 — Second-order features & presentation (the gaps the fresh pass still hit)

The missing features and remaining papercuts from `docs/QA-fresh.md`. Lower priority than prd/41's
correctness, but these are what keep the *comparative* questions ("who is the constraint", "what
should we change first") from being answerable in the UI.

## Features (M1–M8)

- **M1 — response-time comparison** (HIGH): the "who is the constraint" answer needs a per-person
  response-time view — Frank's ~20 h vs peers' ~2.5 h after being added as a reviewer, and last-peer→
  sentinel approval. Add it to the Queue/people view (a "response time" column/toggle), so the sentinel
  being 8× slower than peers is a one-glance fact, not a raw-store computation.
- **M2 — ticket benchmark** (MED-HIGH): the ticket page states where this story sits — "73rd of 87
  fastest; median 89.7 h; 8-pt median 109 h" — so "73 h" is read against its cohort.
- **M4 — "Done in Jira, PR open" list** (HIGH): a first-class list of tickets that are Done in Jira with
  an open/unmerged PR (DEL-167, DEL-168, DEL-183), with ticket keys and who owes each. Surface the
  "PR opened after the ticket closed" case (DEL-183: closed 9 days before its PR opened).
- **M6 — PR drawer completeness** (MED): the drawer shows reviewers/approvals/open threads/last event
  instead of `—` for waiting-on-author/CI/rebasing and no-events-since.
- **M8 — size-vs-cycle** (MED): next to the PR-size sliders, show size vs cycle (the correlation is
  weak, r≈0.2) so "cap PRs" isn't mistaken for a proven lever.
- **M3 — policy knobs** (MED): add refinement (created→ready) and review-round-cap knobs to the lab, so
  two of the four options a lead names can actually be tested.
- **M5 — sprint scope-creep** (MED): show "added after start" (25 of 30 in S2) and the carry-over
  *destination* (stories that never reappear in the next sprint).
- **M7 — pseudonym legend** (LOW): identify the sentinel/roles (who is Frank) without breaking prd/02.

## Presentation papercuts (B2, P2–P11)

- **B2** the delivery row-click drawer covers the inspector (the panel with the best facts). Stack them
  instead of overlapping.
- **P2** severity ≠ rank (critical rules ranked below warnings by impact×confidence) — either explain it
  or make severity break ties.
- **P3** "overloaded/slow" is relative to the window median (4 of 6 people "overloaded" by construction) —
  state the basis.
- **P6** one PR, five durations (#53: "21 d", "24 d", "192 h", "34 calendar days", "29 calendar days") —
  one canonical duration per fact.
- **P7** duplicated "· business hours · · business hours" and the clipped people-chip — clean text.
- **P8** rule examples carry no ticket keys — make them `#ticket/KEY` links like every other view.
- **P9** the epic page is not first-class (a story with no STORY object shows "0/9 segments") — either
  support epics or state clearly it isn't a story.
- **P10** stale URL params after a sprint lens (sprint stays in the URL on Queue/Blocking/Rules) — clear
  the lens when the view doesn't use it.
- **P11** jargon ("unobserved", "sentinel waited on ready") — tooltips/labels.

## Definition of done

Each feature has a test; each papercut is one small change. Conform to `docs/concepts.md`. Keep the
suites green.

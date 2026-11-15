# 03 — Data model: object-centric event log

## Why not a flat case-ID log

Classic process mining assumes one case ID per trace. Our process violates that
everywhere:

- one **story** has **several PRs**,
- one **PR** has **several revisions** and **several reviewers**,
- stacked **PRs** depend on each other,
- **questions** live outside any single ticket (descriptions, comments, Slack,
  PR comments).

Flattening all of this onto the story destroys the review/rebase structure that
is the actual point. So we model it as **object-centric** (OCEL 2.0), where an
event references a *set* of objects rather than one case.

## Object types

| Object | Key fields | Notes |
| --- | --- | --- |
| `Epic` | key, summary-class, team | summary-class only, never raw summary |
| `Story` | key, epic, points, created, resolution | parent/child via issue links |
| `Sprint` | id, start, end, complete, goal-class | carry-over from sprint membership changes |
| `OpenQuestion` | id, phase, asker, answerer, timestamps | one object per question; synthesised by classifier |
| `PR` | id, repo, author, source/target ref, created, merged/declined | |
| `PRRevision` | id, pr, push_index, head_commit, patch-id, push_class | one per rescope |
| `ReviewThread` | id, pr, anchor-class, severity, state, resolved_by | severity enumerated, anchor *classified* not raw |
| `Commit` | hash, repo, author, message-class | message-class = label, never message |
| `Person` | pseudonym, role-class, working-hours calendar ref | role-class (sentinel/dev/…) enumerated |

Every object's human-authored fields are replaced by `*-class` enumerated labels
in the pseudonymised store (see `02-pseudonymization.md`). Nothing in this table
stores a description, comment body, message, or diff.

## Event shape

An event is a row:

```
(ts, activity, actor, related_objects, attributes)
```

- `ts` — timestamp (normalised to one timezone).
- `activity` — from the closed activity vocabulary.
- `actor` — a `Person` pseudonym, or a synthetic actor (`CI`, `calendar`,
  `bot:<name>`) for non-human waits.
- `related_objects` — the set of object IDs this event touches (e.g. a push
  touches `PR`, `PRRevision`, `Story`, `Commit`, `Person`).
- `attributes` — closed, typed attributes (severity, push_class, phase, points,
  sprint id, …).

Synthetic actors matter: waiting on CI, or waiting for the weekly refinement
meeting, is not waiting on a person. Modelling those as actors makes the
blocking graph honest.

## Example rows

```
ts                     activity            actor  objects                        attributes
2026-11-03T09:12:00Z   pr_pushed           LaNo   {PR-482, PRRev-3, STORY-12}   push_class=content
2026-11-03T09:40:00Z   pr_approved         ToBr   {PR-482, PRRev-3}             sentinel=true
2026-11-04T14:05:00Z   question_raised     LaNo   {STORY-12, QUESTION-7}         phase=implementation
2026-11-05T10:00:00Z   question_answered   MaMü   {STORY-12, QUESTION-7}         phase=implementation
```

## Storage

- **Primary store**: DuckDB (local OLAP, zero-ops) with Parquet backing for the
  event log and object tables. Cheap, fast, portable, and — critically — the
  *schema* is the pseudonymisation contract (no text columns).
- **Event-building logic in SQL**: the transformation from per-source extracts
  to OCEL events lives in auditable SQL (dbt-style, or plain `.sql` files run in
  order). A human can read exactly how `pr_pushed` was derived. No hidden
  imperative steps.
- **Reviewability**: SQL + versioned classifier config = a fully reproducible
  pipeline from raw → pseudonymised, reproducible without retaining raw data.

## Identity resolution

A table in the **raw zone** maps `Jira Cloud accountId (incl. Tempo authors) ↔ Bitbucket slug ↔ git email ↔
Slack ID → pseudonym`. Downstream, only `pseudonym` and `role-class` propagate.
This is what makes "the same person" consistent across a Jira assignee, a git
author, and a Slack user.

## Working-hours calendar

A per-person calendar (holidays, part-time, timezone) so that every wait is
measured in **business hours**. Without it, weekends dominate every number and
make the analysis meaningless. Calendar data is pseudonymised with the same
`Person` key.

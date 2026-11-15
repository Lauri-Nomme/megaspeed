# 04 — Exports: Jira, Bitbucket, Slack

Everything below is *what we pull* and *why*. What we **retain** downstream is
only the pseudonymised subset per `02-pseudonymization.md` — the raw text listed
here is read by the local classifier and discarded.

## Jira

| Export | Fields | Why it matters |
| --- | --- | --- |
| Issue snapshot | key, type, parent/epic, story points, assignee, reporter, created, resolution, labels, components, issue links (esp. "blocks"/"is blocked by") | builds the Epic/Story objects and the dependency DAG |
| Changelog (`expand=changelog`, paginated) | every change to status, assignee, sprint, points, description, Flagged — with author + ts | status transitions → activities; sprint changes → carry-over; point changes → estimate revision |
| Description edit history | edits to the description field | goals often get clarified by editing, not commenting |
| Comments | author, created, updated, body | body is *read by classifier only* → question/severity events; never stored |
| Worklogs | author, `started`, `timeSpentSeconds`, `created` | the `created − started` gap measures worklog *noise*; a cross-check, not a source of truth |
| Sprints (Agile API) | start, end, complete, goal, membership-at-start vs added-later | carry-over and mid-sprint insertion |
| Dev-status / remote links | story → PR / branch / commit mapping | the glue between ticket layer and PR layer |
| Refinement calendar | a list of meeting dates/times | separates "waiting for the next refinement" (batching delay) from actual work |

## Bitbucket (Data Center)

| Export | Why it matters |
| --- | --- |
| PR activities stream (`/pull-requests/{id}/activities`) | **the most important data.** OPENED, RESCOPED (each push: old/new hashes, added/removed commits), COMMENTED, APPROVED, UNAPPROVED, NEEDS_WORK, reviewer changes, MERGED, DECLINED |
| PR metadata | author, reviewers + status, source/target ref, created/closed ts |
| Comments as threads | anchor (file/line) *classified*, parent/replies, severity (BLOCKER/task vs normal → mandatory vs suggestion), resolved-by/ts |
| Per-revision diff stats | files/lines changed per rescope — not just the final diff |
| Build status per commit | CI wait is a hidden queue; model it as a synthetic actor |
| Merge check config + sentinel membership | so we can evaluate the merge condition at any point in time |
| Target-branch relationships | a PR whose target is another PR's source branch is **stacked** → the dependency DAG |
| Local git clone per repo | to classify pushes ourselves (below) |

### Push classification (deterministic)

For each RESCOPED event:

```
git diff target...old | git patch-id --stable
git diff target...new | git patch-id --stable
```

Three labels:

1. **`content`** — real rework (patch-id changed).
2. **`pure_rebase`** — same patch-id; if approvals were lost here, that's pure
   waste.
3. **`rebase_conflict`** — patch-id changed, but `git range-diff` shows only
   small conflict hunks.

One concrete, high-value finding to verify on our instance: Atlassian's
**Auto-Unapprove** plugin by default only unapproves when the patch-id changes,
so pure rebases should *keep* approvals. But it also resets approvals if the
patch-id computation exceeds ~20 s. If our data shows approvals lost on
`pure_rebase`, it's one of those two behaviours — and probably a config fix
(`plugin.bitbucket-pr-auto-unapprove.gitrescopeanalyzer.patchid.enabled=false`).

## Slack

| Export | Why it matters |
| --- | --- |
| Messages + thread replies in team channels | `ts`, `thread_ts`, user, text, edited, reactions |
| Ticket/PR linkage | regex for ticket keys and PR URLs first; LLM linkage for the unlinked remainder (by time-window + participants) |
| Bot messages (Jira/Bitbucket) | cross-check timestamps; **excluded** from human-latency metrics |
| DMs | hold much of the blocking context but are sensitive → **metadata only** (who, when, thread length), with consent, or skipped |

DM text, like everything else human-authored, is either never pulled or pulled
locally and reduced to metadata + labels — never stored.

## Cross-cutting

- **Identity resolution table** (raw zone only) — see `03-data-model.md`.
- **Working-hours calendar** — per person, business-hours waits.
- **Bot account set** — to filter non-human actors before any latency metric.

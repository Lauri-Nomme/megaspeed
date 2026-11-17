# Adversarial review, round 2 — the six un-reviewed features

**Scope (new since `docs/review-adversarial.md`):**

1. Jira ingest — `ingest/scrape_jira.py`, `ingest/jira_pipeline.py`, `ingest/jira.py`,
   `ingest/pipeline.py` (EPIC/STORY/SPRINT, status/sprint/assign/point/worklog, identity)
2. Jira-layer analysis — `analysis/jira.py`
3. Flow efficiency + survival — `analysis/rules.py` (`flow_efficiency`, `time_to_merge`),
   `analysis/survival.py`
4. Scrape reliability — `ingest/transport.py`, `ingest/scrape.py`
5. Stacked-PR cascade — `ingest/pipeline.py` (`ref_digest`), `analysis/stack.py`
6. Simulation / policy lab — `analysis/simulation.py`, `server/app.py` (`/api/simulate`),
   `app/policy.js`

Reviewed at `083e0b9` (clean tree). **Method:** read each PRD against its code, then ran
it — Jira fixture scrape → tgz → import → `/api/insights`, the Bitbucket fixture into the
same store, a combined store, a simulated `SIGKILL` mid-scrape followed by a resume, a
hand-built stack DAG and a hand-built cascade, the KM estimator on degenerate samples,
and `/api/simulate` driven through the real ASGI app with adversarial query strings.
Nothing in the repo was modified except this file.

**Baseline:** `tests/` 504 OK (40 s), `scrape/tests` 65 OK, `app/test/*.mjs` 57 pass.
`--verify` passes on the Jira fixture import ("no free text, no canaries").

## Verdict

The analysis layer is the strongest code in the repo: Kaplan–Meier is implemented
correctly (ties, censoring and the event-before-censor convention all check out), the
flow-efficiency accounting identity holds exactly on `del3`, the simulation scales
linearly (no O(n²)), checkpoint/resume really does survive a kill — including the
relations, which ride inside each event row. The closed vocabulary is enforced at the
parse layer, not by convention.

The failures are at the edges, and three of them are the kind that produce *confident
wrong answers* rather than errors:

- the **Jira watermark is advanced by issues that were never fetched**, so a capped run
  silently deletes history (FR-1) — and one bad issue kills the whole run with no
  checkpoint (FR-2);
- the **simulation deadlocks on a declined stack parent**, runs 28× past its horizon,
  and then divides every utilisation figure by that inflated span — the number the whole
  "attention latency, not more reviewers" conclusion rests on (FR-3, NFR-1);
- **`ref_digest` does not protect the branch name it exists to hide**: an unsalted,
  unkeyed 64-bit hash of a low-entropy slug is reversible in milliseconds (SEC-1).

Severity counts: **4 High, 3 Med-High, 8 Medium, 12 Low**.

| ID | Sev | One line |
| --- | --- | --- |
| FR-1 | High | Jira `--limit` run advances the watermark past issues it never read: 8 of 13 lost forever |
| FR-2 | High | One 404 on one issue aborts the whole Jira scrape; no per-issue isolation, no checkpoint |
| FR-3 | High | Simulated stacked child waits forever on a declined parent; run goes 28× past horizon |
| SEC-1 | High | `ref_digest` is a reversible dictionary hash of the branch name (= ticket summary) |
| NFR-1 | Med-High | Utilisation divides by a span that includes the unbounded drain → saturation is hidden |
| FR-4 | Med-High | Cascade attributes a rebase 44 days later to a lower-PR merge |
| FR-5 | Med-High | Stack DAG invents edges from shared base branches (a `main→prod` PR parents every PR) |
| FR-6 | Medium | `/api/simulate` accepts `replications`, echoes it, never uses it |
| FR-7 | Medium | `calibrate(bots=None)` default puts bots in the reviewer pool → model saturates, gate fails |
| NFR-2 | Medium | Jira scrape re-fetches every sprint's membership every run, unbounded |
| NFR-3 | Medium | `sentinels: 20000` → 1.25 s and a 3 MB response, no cap; 64-entry cache of them |
| SEC-2 | Medium | Cross-source identity merges humans on a shared display name; identity table 0644 next to the stores |
| FR-8 | Medium | `--limit` is silently ignored in Jira fixture mode |
| DES-1 | Medium | "of ticket cycle time" is computed against the decomposed total, not the cycle (76 % vs 33 %) |
| DES-2 | Medium | `available:false` diagnoses are wrong for a store with no PRs / no Jira |
| FR-9…FR-12, NFR-4…NFR-6, SEC-3, DES-3…DES-6 | Low | see sections |

---

## FR — functional holes

### FR-1 · **High** · A capped Jira run consumes the watermark for issues it never fetched

`ingest/scrape_jira.py:285-317`. The watermark is accumulated from **every row the search
returns**, before the `limit` check, and the loop keeps walking the remaining pages after
`stop = True`:

```python
for row in client.search(jql):
    ...
    updated = _updated(row)
    if updated is not None:
        stats.watermark = updated if stats.watermark is None else max(stats.watermark, updated)
    if not key_filter.accepts(key): ...
    if limit is not None and stats.issues_kept >= limit:
        stats.issues_skipped += 1
        stop = True
        continue                      # <-- keeps counting, keeps advancing the watermark
```

`scrape()` then persists `watermark = max(previous, collected.watermark)`
(`scrape_jira.py:516`), and the next JQL is `updated >= watermark - 72 h`
(`jql_for`, `scrape_jira.py:163-179`). Everything below the cutoff is never asked for
again — and because those issues have no `jira_issues.json` entry, the per-issue
`_unchanged` guard cannot save them either.

Repro (one fixture bundle, one state dir, `limit=3` then a normal run):

```
run1: kept 3  watermark 2027-01-05T16:00:00Z  events 6
run2: seen 2  kept 2  skipped 0               watermark 2027-01-05T16:00:00Z
bundle events 11
reference (no limit): kept 13 events 68
issues lost forever: 8
```

The Bitbucket side already has the guard this needs (`scrape.py:660`,
`if not truncated and newest is not None`). The Jira side has no equivalent, and the
manifest does not record that the run was capped (`build_manifest` has no `truncated`).

**Fix:** stop accumulating `stats.watermark` once `stop` is set (or `break` out of the
search iterator instead of `continue`), record `truncated: true` in the manifest and the
report, and skip the watermark write when a run was capped — mirroring
`scrape.py:660-661`. Add the regression test: `collect(limit=n)` then a second run must
still see all issues.

### FR-2 · **High** · One failed issue aborts the whole Jira scrape, and there is no checkpoint

`ingest/scrape_jira.py:306` — `issue = client.issue(key)` is not wrapped in `try/except`.
A single deleted/moved issue, a permission error, or a 500 that outlives the transport
retries raises `JiraError` out of `collect()` and kills the run. The Bitbucket side
isolates this per PR (`scrape.py:542-624`, `failures` + `continue`), which is exactly
the reliability work this review is checking.

Repro (fixture transport that 404s one issue):

```
WHOLE SCRAPE DIED: JiraError GET /rest/api/2/issue/PROJ-103 failed: 404 issue does not exist
```

And the Jira side has **no crash resume at all**: `JiraIssueState.save()` is only called
after the bundle is written (`scrape_jira.py:532`), so a kill after two hours of
per-issue requests loses every per-issue cursor. Contrast `ingest/scrape.py:1175-1203`,
which checkpoints identity → bundle → `prs.json` → `watermark.json` every N PRs. The
Jira scrape costs 1-3 requests per issue; this is the expensive half.

**Fix:** wrap the per-issue fetch exactly like the Bitbucket per-PR block (record
`{key, error}` in `JiraCollected.failures`, `continue`, and do not record the cursor so
the next run retries); and checkpoint `jira_issues.json` + the partial bundle on an
interval (`--checkpoint-every` already exists on the CLI but is not plumbed to the Jira
path).

### FR-3 · **High** · A simulated stacked child waits forever on a declined parent

`analysis/simulation.py:1228-1243`:

```python
if pr.parent is not None and pr.parent.merged_at is None:
    while pr.parent.merged_at is None:
        yield env.timeout(0.25)
```

A parent that is **abandoned** (`pr.abandoned = True`, `finished_at` set, `merged_at`
never set, `simulation.py:1215-1219`) can never satisfy that predicate. The child polls
every 0.25 business hours until `run()`'s drain guard gives up
(`simulation.py:1256-1260`, 20 000 × 1 h).

Repro (`del3` calibration with a synthetic stack-depth histogram `{1:1, 2:1}`, decline
0.17, horizon 720 h, one replication):

```
elapsed 0.26s  span(h) 20720.0  prs 33  merged 23  declined 5
unfinished: 5
children with abandoned parent: 5
env.now 20720.0            # horizon was 720
```

So: 5 PRs can never finish, the run burns 20 000 extra event steps, and those PRs are
then reported as `censored` (`_summary`, `simulation.py:1358`) as if they were merely
still open. A longer chain stalls every PR above it. (The parent link itself is also
loose — `_last_by_depth` is keyed by depth only, so a child can be parented to an
unrelated PR from another author/stack, `simulation.py:1157-1163`.)

**Fix:** have the child wait on a SimPy event that the parent triggers on **either**
outcome (`merged_at` or `abandoned`), and treat "base abandoned" as a modelled outcome
(decline the child, or re-base it) instead of an infinite wait. Key `_last_by_depth` by
(author, stack root) so a stack is a stack.

### FR-4 · **Med-High** · The cascade attributes a rebase arbitrarily far in the future

`analysis/stack.py:192-199` — `_response_push` returns the *first* push at/after the
trigger with **no upper bound** and no requirement that nothing else happened in between:

```python
for event in events:
    if event["_ts"] < after: continue
    if event["activity"] in TRIGGER_PUSHES: return event
```

Repro (parent merged at +24 h; the child's only rebase is 44 days later):

```
triggers 1  pairs 1
attributed: {'trigger': 'merge', 'at': 2026-01-06 09:00,
             'rebase_at': 2026-02-19 09:00, 'hours': 6.0, 'depth': 1}
```

Six hours of re-review charged to a merge six weeks earlier. The cascade total
(`re_review_hours`) and the `stacked-cascade` rule ("cap stack depth") both inherit it.

**Fix:** bound the response window — the rebase must be the child's next event after the
trigger (or within the child's own review-round budget), and the child must still be
open and un-rebased since the trigger. At minimum cap at one business day and report
`unattributed_triggers` when the cap is hit.

### FR-5 · **Med-High** · The stack DAG invents edges from shared base branches

`analysis/stack.py:141-176` builds `by_source[source_ref] → PRs` over **all** PRs and
then links every PR whose `target_ref` matches. There is no default-branch guard and no
temporal check.

Repro (one release PR `main → prod`, plus five ordinary feature PRs targeting `main`):

```
release-PR-from-main edges: [('PR:DEL/web#1','PR:DEL/web#2'), ... ('#1','#6')]
depth: {'#1': 0, '#2'..'#6': 1}
components: [['#1','#2','#3','#4','#5','#6']]
```

Five unrelated PRs become "1 stack of 6, 5 stacked PRs" — and the rule fires. A child
created 29 days **before** its "parent" is also accepted (edges are time-blind; only the
cascade step checks openness).

**Fix:** exclude the repo's default/target branches (`main`, `master`, `develop`, the
most common `target_ref`) from `by_source`; require `parent.created_ts <= child.created_ts`
and (for a stack) `parent.closed_ts is None or parent.closed_ts > child.created_ts`;
report `ignored_shared_branch_edges` so the exclusion is visible.

### FR-6 · **Medium** · `/api/simulate` accepts `replications`, echoes it, and ignores it

`server/app.py:920-923` parses and validates `replications`, includes it in the cache key
and in `payload["params"]` (`app.py:966-970`) — but `policy_lab.simulate(...)`
(`app.py:960-962`) takes no `replications` argument, so `run()` always uses
`DEFAULT_REPLICATIONS = 6`.

Repro: `GET /api/simulate?tenant=del3&replications=1`

```
params   {'replications': 1, ...}
spread   {'replications': 6, 'median_min': 16.82, 'median_max': 22.51, ...}
```

A caller who asks for a fast single replication pays for six and is told it got one.
`provenanceLine` in the UI prints `spread.replications`, so the UI is honest; the API
contract is not.

**Fix:** thread `replications` through `simulate()` → `run()` (it already accepts it), or
reject the parameter with a 400 rather than silently ignoring it (BUG-13's own rule:
"every query parameter is either honoured or rejected").

### FR-7 · **Medium** · `calibrate()`'s default puts bots in the reviewer pool

`analysis/simulation.py:592` — `bots = set(bots or ())`; the pool is
`samples["review"]` minus bots (`simulation.py:616`). Only `server/app.py:957-959` passes
`bots=insights_rules.load_bots(con)`. Call `calibrate(court, window, conn=con)` the
obvious way and the "Bitbucket" service account becomes a capacity-1 reviewer:

```
bottleneck {'person': 'BiBi', 'role': 'reviewer', 'utilisation': 0.9764,
            'mean_queue': 10.93, 'mean_wait_hours': 687.79}
busy/rep 4474.2 h  vs horizon 2174.4 h        # 206 % of the available time
predicted median 111.11 h  p90 1886.39 h      # history: 24.77 h
warnings ['BiBi is saturated (utilisation 98%) ...']
```

With `bots=load_bots(con)` the same store gives median 21.97 h and the gate passes
(−11.3 %). So the difference between "the model validates" and "the model is nonsense"
is one argument at one call site.

**Fix:** default `bots=None` to "read `actor_kind` from the store" (the connection is
already passed in), and make excluding a bot from the pool non-optional; keep an explicit
`include_bots=True` for debugging.

### FR-8 · **Medium** · `--limit` is silently ignored in Jira fixture mode

`ingest/scrape_cli.py:104` — the fixture branch returns `None` as the limit
(`return FixtureJiraTransport(bundle), "jira-fixture", None`) while the live branch
returns `args.limit`. `--jira --fixture --limit 3` therefore reads all 13 issues. Small
in itself, but it is the flag that would have exposed FR-1 during development.

**Fix:** return `args.limit` in both branches.

### FR-9 · **Low** · A segment bounded by a touch can never be credited that touch

`analysis/jira.py:509-511` credits a touch only when `t1 < stamp < t2` — a boundary event
"belongs to neither side". The refinement boundary *is* the first `pointed` event
(`_cycle`, `jira.py:412`), so `ready → refined` can only earn work from PR intervals or
worklogs. On the Jira fixture:

```
ready_refined: hours 480.0  active_hours 0.0  efficiency 0.0  n 10
refined_sprint: hours 5823.0 active_hours 0.0 efficiency 0.0  n 8
```

0 % flow efficiency for refinement is a structural artefact, not a measurement. It is
also the segment the `jira-waiting-time` rule will report as "nothing is being worked on
during it" — which for refinement is false by construction.

**Fix:** credit the boundary touch to the segment it *ends* (or state
`efficiency_basis: "spans only"` for segments whose left boundary is a touch), or treat
refinement as a priced activity (`TOUCH_HOURS`) rather than a queue.

### FR-10 · **Low** · The "of ticket cycle time" headline uses the wrong denominator

`analysis/jira.py:593-625` computes `share = hours / total_hours`, where `total_hours` is
the sum of the eight segments; `cycle_hours` (created → done) is reported separately. The
rule text (`rules.py:1869-1872`) says "**of ticket cycle time**" and stores the
denominator under the key `"cycle_hours": wtd["total_hours"]` (`rules.py:1875`).

On a Jira-only store (`/tmp/jtest/jira.duckdb`):

```
largest refined_sprint  share 0.7952  (5823 / 7323)
cycle_hours 17631.64    explained_share 0.4153
true share of the cycle: 0.330
```

The headline says 76 % (in `/api/insights`) where the honest number is 33 %. When the
store also has the PR half the two converge (`explained_share 0.9728`), so this bites
exactly the ticket-only tenant the Jira ingest exists to serve.

**Fix:** use `hours / cycle_hours` for the share (and keep `explained_share` next to it),
or rename the label to "of measured segment time" and the evidence key to
`segment_total_hours`. Two different values must not share the key `cycle_hours` in one
payload.

### FR-11 · **Low** · `critical` rules with `impact_hours = 0.0` rank last

`rules.py:1976`, `1996`, `2022` pass `0.0` as the impact for `jira-worklog-noise` and
`jira-worklog-mismatch`, while the severity can be `critical` (`rules.py:1952-1956`,
`2002-2005`). Ranking is `-impact_score` first (`rules.py:2078-2081`), so on the combined
store:

```
- jira-worklog-mismatch  critical  impact=0.0   (rank 9)
- jira-worklog-noise     critical  impact=0.0   (rank 10, last)
```

A critical badge at the bottom of the list. These are diagnostics, not "change this"
rules; either give them an impact basis (e.g. `untrusted_hours` for the noise rule) or
emit them as `severity: info` / a separate `diagnostics` block.

### FR-12 · **Low** · UI prints `stack depth ≈0% measured` whenever refs exist

`app/policy.js:294`:

```js
['stack depth', stack.available ? `${predPercent(0)} measured` : 'not measurable (no ref digests)'],
```

`predPercent(0)` is the string `≈0%`, so a store with a median depth of 2 and 12 stacked
PRs renders "stack depth ≈0% measured". Verified with the real function:

```
<dt>stack depth</dt><dd>≈0% measured</dd>
```

The node test only covers `available: false` (`app/test/policy.test.mjs:33`).

**Fix:** `${stack.stacked_prs} stacked · median ≈${stack.median} deep`, and pin the
`available: true` branch in the test.

---

## NFR — performance, scale, reliability

### NFR-1 · **Med-High** · Utilisation is divided by a span that includes the unbounded drain

`analysis/simulation.py:1306-1321`:

```python
busy_hours = max(1e-9, pooled.get("span", ...))     # span = Σ max(horizon, env.now)
... "utilisation": round(entry["busy"] / busy_hours, 4)
```

`span` is `max(self.horizon, self.env.now)` per replication (`simulation.py:1282`), and
`env.now` grows past the horizon by however long the drain takes — which is unbounded up
to the 20 000-step guard, and is *largest exactly when a queue diverges*. The docstring's
intent ("a saturated server reads ~1.0 and never above it") is achieved by inflating the
denominator, not by measuring saturation.

Evidence, one replication of the `del3` baseline (horizon 2174 h):

```
span 4217.4 h  (1.94 × horizon)
sentinel:1  busy/span = 0.336   busy/horizon = 0.652
```

and the deadlock case (FR-3): span 20 720 h vs a 720 h horizon → a **28.8×** deflation,
so a server that is saturated reads 3 %. Because `_summary` derives both the `warnings`
list (`utilisation >= 0.9`, `simulation.py:1384-1389`) and the `bottleneck` pick
(`simulation.py:1324`) from this number, a diverging queue can suppress the very warning
it should raise. It is seed-dependent: the same store at 2× arrivals gave
`utilisation 0.251` with `warnings []` while p90 was 1492 h.

**Fix:** divide busy time by the modelled horizon (per replication, summed), not by the
drained span; report `drain_hours` separately, and warn when `drain_hours > horizon/2`
(the run did not settle). Add a test that pins `utilisation` against a hand-computed
saturated server.

### NFR-2 · **Medium** · The Jira scrape re-fetches every sprint's membership on every run

`ingest/scrape_jira.py:240-253`:

```python
for sprint in client.sprints(board):            # state=future,active,closed — all of them
    ...
    for key in client.sprint_issue_keys(sid):   # one request per sprint, every run
```

There is no cursor, no date filter and no `--since`. On a long-lived project this is
O(all sprints ever) requests **per run**, forever. The fixture needs 25 requests for
13 issues / 3 sprints, of which 7 are sprint work (`boards=3, sprints=1,
sprint-issues=3`); on an incremental run, where most issues are skipped, those 7 are the
floor of every scrape. The Bitbucket side has a per-repo `updatedDate` cursor precisely to
avoid this.

**Fix:** page sprints from the watermark (`startDate >= watermark - N` or the board's
active+future + the last K closed), cache sprint metadata by id in the state dir, and only
re-read membership for sprints whose `completeDate` moved.

### NFR-3 · **Medium** · `/api/simulate`'s scenario knobs are unbounded (CPU, memory, response size)

`analysis/simulation.py:371-380` (`_positive_int`) has no upper bound, and
`sentinels` allocates one SimPy `PriorityResource` per slot plus one resource row in the
response (`simulation.py:1046-1049`, `1272-1280`). Measured through the real ASGI app on
`del3`:

```
sentinels   2000 → 0.27 s,   312 958 byte response, 2 006 resource rows
sentinels  20000 → 1.25 s, 2 986 961 byte response, 20 006 resource rows
```

Linear in the knob with no ceiling. `SIM_CACHE_SIZE = 64` (`server/app.py:899`) then holds
up to 64 such payloads (~200 MB at 20 k sentinels). `seed` is unbounded
(`app.py:919`) and the cache key uses the **raw** `scenario` string (`app.py:947`), so
`{"wip_limit":1}` and `{"wip_limit": 1}` are two cache entries — trivial cache-busting,
and `name` is an unbounded string that is echoed into the key and the response.

**Fix:** cap `sentinels` (e.g. ≤ 10), cap `name` length (≤ 64, shape-validated like
`CLASSIFIER_TOKEN`), key the cache on `scenario.as_dict()` + a digest, and cap the
`resources` list in the payload (top N + "and 19 994 more").

### NFR-4 · **Low** · Transport retry: no `Retry-After`, no jitter, and it sleeps before giving up

`ingest/transport.py:127-149`. 429 is retried with `time.sleep(1.5 ** attempt)` and the
`Retry-After` header is ignored — DC publishes it, and ignoring it is how a client gets
rate-limited harder. There is no jitter, so N parallel scrapes retry in lockstep. The
final attempt also sleeps 2.25 s *before* the `raise`, so a permanently failing path pays
its full backoff and then fails anyway.

**Fix:** honour `Retry-After` on 429/503 (capped), add `random.uniform(0, 0.5) * 1.5**n`,
and skip the sleep on the last attempt.

### NFR-5 · **Low** · Store-wide scans in the Jira loader

`analysis/jira.py:300` (`SPRINT_LINK_SQL`) reads **every** sprint↔event link in the
store with no window filter, into a Python dict; `worklog_cross_check` then walks all
stories. At `del3` scale (420 events) this is nothing; at the million-event scale prd/14
targets it is a full-table pass per `/api/insights` request. `STORY_EVENT_SQL` is at least
restricted to `object_type='STORY'`.

**Fix:** restrict both to the window's object ids (`WHERE object_id IN (...)`) or push
the join into one SQL statement.

### NFR-6 · **Low** · A PR that names two tickets counts its active hours twice

`analysis/jira.py:848-853` sums `state.intervals` for every `story.prs`, and a PR with
`ticket_keys = [A, B]` appears in both stories. `worklog_cross_check.active_hours` (and
therefore `logged_to_active_ratio`, which can emit a `critical` rule) is inflated by
exactly the multi-ticket PRs. Deduplicate by PR object id.

---

## SECURITY

### SEC-1 · **High** · `ref_digest` is a reversible dictionary hash of the branch name

`ingest/pipeline.py:90-107`:

```python
return hashlib.blake2b(f"{repo}|{branch}".encode(), digest_size=8).hexdigest()
```

The module docstring is explicit that this exists because "a DC branch name is the ticket
summary in slug form" and prd/02 forbids summaries. But the digest is **unsalted, unkeyed
and 64-bit**, and its two inputs are both in the clear in the same store: `repo` is the PR
object's `repo` attribute, and the ticket key is `PR.ticket_keys`. Branch names are drawn
from a tiny, guessable space (`KEY-<slug of the summary>`).

Repro (repo `DEL/web`, ticket `DEL-101`, permutations of 11 common words up to length 4):

```
candidates tried: 9031 -> recovered: DEL-101-add-login-form-validation
```

Milliseconds, from the store alone. The digest is also **stable across stores and
tenants** (no salt), so two exports of the same repo correlate PR-by-PR, and a
precomputed table for a repo is reusable forever. `REF_DIGEST` shape validation
(`taxonomy.py:117`) stops a raw name being *written*, but not the name being *recovered* —
so the prd/02 claim "the branch name never crosses the boundary" is false in practice.

**Fix:** HMAC-SHA256 (or blake2b with `key=`) using a per-tenant pepper that lives in the
raw zone and never enters the store or the bundle, truncated to ≥ 128 bits. Document the
residual leak that is *inherent*: equal digests reveal "same branch" (that is the
feature) and nothing else. Add a test asserting the digest changes when the pepper
changes.

### SEC-2 · **Medium** · Cross-source identity merges humans on a shared display name, and the table lives next to the stores

`ingest/pseudonym.py:168-197` links a new actor to an existing one from the *other*
source when exactly one candidate shares **any** token: the username, the full email, the
email local part, or the normalised display name. Two different people called "John
Smith" — or two people whose usernames are both `jsmith` on different systems — are
merged silently into one pseudonym (`LaNo`). The docstring says "an ambiguous match
allocates a fresh pseudonym"; a single false match is not ambiguous to the code.

Separately: the identity map is a plain JSON file with mode **0664** (group-writable,
world-readable) that holds `display`, `email`, `given`, `surname` in clear. The repo
currently has two of them — `var/identity_map.json` (the `cfg.identity_path` default) and
`data/identity.json` — and `data/` is also the default `TENANTS_DIR`. The lab names are
synthetic, but the same shape is what a real scrape writes, and the one irreplaceable
raw-zone artefact then sits in the directory the server serves tenants from.

**Fix:** require the *strong* signal (exact email) for cross-source linking, or require
two independent tokens; log every link decision into the raw zone so a wrong merge is
auditable; write the identity map 0600 and keep it outside the tenant directory, and
refuse to start if it is group/world-readable.

### SEC-3 · **Low** · `/api/simulate` returns the whole calibration object

`analysis/simulation.py:759-922` is echoed verbatim (`simulate()` → `payload["calibration"]`).
Everything in it is pseudonyms, closed labels and numbers — so no prd/02 violation — but it
is a large, per-person behavioural profile (per-reviewer service fits, utilisation, queue
lengths) served on an unauthenticated read endpoint. Worth a deliberate decision: the
`level=team` / `level=person` split that `/api/insights` enforces does not exist here, so
`/api/simulate` is always "person level".

---

## DESIGN — coherence with the closed vocabulary and the object model

### DES-1 · **Medium** · "of ticket cycle time" mixes two denominators (see FR-10)

Already covered above; listed here because it is a *model* problem, not a typo: the
decomposition is over "the segments we could measure", the cycle is over "the story's
life", and the payload needs to say which is which once, in one place. Today
`metrics.waiting_time_decomposition.cycle_hours` and
`suggestions[jira-waiting-time].evidence.cycle_hours` are two different numbers with the
same name.

### DES-2 · **Medium** · `available: false` diagnoses are wrong for a store with no PRs

`analysis/stack.py:415-427`:

```python
available = graph.refs > 0
if not available:
    note = "no PR carries source/target ref digests: the store predates prd/22, so the
            stack DAG cannot be derived (re-ingest to measure it)"
elif not stacks:
    note = "every PR targets its own branch (or main): the corpus has no stacked PRs"
```

On a Jira-only tenant (`/tmp/jtest/jira.duckdb`, zero PR objects) `/api/insights` says
"re-ingest to measure it" — re-ingesting will not help, there are no PRs. A store with
PRs but no refs and a store with no PRs are different results and need different notes.
Same for `_stack_depth`'s "the store predates prd/22" (`simulation.py:576-581`).

**Fix:** three-way: `no PRs at all` → "the store has no PRs (Jira-only tenant)"; PRs but
no refs → "predates prd/22"; refs but no stacks → the current message.

### DES-3 · **Low** · The synthetic `at_start` membership can predate the story

`ingest/jira_pipeline.py:594-611` emits `sprint_added` at the **sprint's start date** for
any issue currently in the sprint with no Sprint changelog item. A story created *inside*
an active sprint has no such changelog item, so it gets a `sprint_added` timestamped
before the story existed. The `at_start: true` attribute is on the event but
`carry_over` (`analysis/jira.py:672-700`) ignores it: `first_added = min(...)` then
`wait_hours` from that instant, overstating the "commitment that did not turn into
progress" by however long the sprint had already been running. `waiting_time_decomposition`
counts it as an `inverted` segment instead (harmless).

**Fix:** clamp the synthetic timestamp to `max(sprint_start, story_created)`, and have
`carry_over` either skip `at_start` events or use `story.start` as the floor.

### DES-4 · **Low** · `_cycle` mixes boundaries from different PRs of one story

`analysis/jira.py:419-429`: `pr_open = min(...)`, `merged = max(...)`,
`approval = max(pr_approved <= merged)`. For a story with two PRs, `final_approval_merge`
can price the approval of PR #1 against the merge of PR #2 (or come out negative and be
counted as `inverted`). Multi-PR stories are the normal case for anything bigger than a
day. **Fix:** derive the PR-side boundaries from one chosen PR (the last one merged) and
report the others as context, or model per-PR segments and aggregate.

### DES-5 · **Low** · `validate()` compares two different cycle definitions

`analysis/simulation.py:1479-1486` gates the model's `merged_at − started_at` (started
*after* the WIP gate, `simulation.py:1183-1186`) against
`rules.cycle_time`'s `merged − opened`. At baseline they coincide (no WIP limit), so the
gate is meaningful today; with `wip_limit` set the two diverge by exactly the backlog
wait, which the model reports as `lead_hours`. **Fix:** gate on
`lead_hours.median` vs `cycle_time`'s opened→merged median, and keep the internal cycle
for the A/B (where baseline and scenario share the definition).

### DES-6 · **Low** · Sentinel capacity is listed twice, and the sentinel *person* is dead weight

`analysis/simulation.py:1263-1280` emits a resource row for every `calibration["reviewers"]`
entry — including sentinel people, whose `_Server` is never used because `peers` excludes
them (`simulation.py:616-618`) — and again for every `sentinel:N` slot. On `del3`
(`court.sentinels = {'FrMo'}`, bots excluded):

```
sentinel:1  sentinel  0.4997  648 requests
FrMo        sentinel  0.0000    0 requests
```

The same role appears twice, one at 0 % with no traffic, and the real sentinel person's
calendar/service fit never reaches the gate (the gate samples
`calibration["sentinel"]["service"]` on anonymous slots). `sentinels: 2` therefore adds
capacity without adding the people who supply it. **Fix:** emit sentinel people with
`role: sentinel-person` and no utilisation, or drop them from `resources`; and document
(or model) that the gate's slots are not the sentinel's own calendar.

---

## What held up (checked, not assumed)

- **Kaplan–Meier** (`analysis/survival.py`): ties are grouped; an event at the same
  instant precedes the censoring; `at_risk` is decremented after the step; all-censored
  and single-observation inputs return `median: null` / `median_reached: false` rather
  than a fake number. Verified on 7 degenerate samples.
- **Flow-efficiency accounting** (`rules.py:523-712`): on `del3` (no calendar, so
  wall-clock hours), `working + waiting = 24 744.9 h = elapsed + touch_hours` exactly;
  `clamped = 0`; per-PR efficiency never exceeds 1; `waiting_breakdown` shares sum to 1.
- **Checkpoint/resume** (`scrape.py`): a simulated kill mid-walk (`KeyboardInterrupt`
  after 7 transport calls) left `complete: false`; the next run resumed, skipped the 3
  checkpointed PRs, and produced a bundle **identical** to a clean run (0 missing
  events, 0 missing relations, 0 missing objects) — the per-event `objects` list makes
  the missing `relations` carry-over harmless.
- **Simulation scaling**: 30/60/120/240 horizon days → 134/261/532/1045 PRs at a flat
  ~100-123 µs/PR. No quadratic term.
- **Closed vocabulary**: `taxonomy.check_attributes` rejects unknown keys *and* values
  (not just known-but-wrong ones); the only thing the Jira reducer derives from a raw
  payload that is not a closed label is `content_hash` (a digest), and no raw field name
  can reach an event key (`FIELD_TOKENS`, `jira_pipeline.py:47-52`).
- **Transport retry**: `ConnectionResetError`/`BrokenPipeError` are retried (they are
  `OSError`, not `URLError` — the bug this was written for), 404 is not.

## Suggested order of work

1. FR-1, FR-2 — the Jira cursor and per-issue isolation. Data loss and a fragile
   two-hour run; both are small, local changes with clear Bitbucket precedent.
2. SEC-1 — pepper the ref digest. Cheap now, expensive after a real store is exported.
3. FR-3 + NFR-1 — the stack-wait event and the utilisation denominator. These two
   together decide whether the policy lab's headline claim is trustworthy.
4. FR-4, FR-5 — the cascade bounds. The feature is currently unused on real data, which
   is the cheapest possible time to fix its attribution.
5. FR-6, FR-7, DES-1, FR-10 — the honesty fixes: honour `replications`, make bot
   exclusion the default, and give the decomposition one denominator.
6. Everything else is polish; DES-2 and FR-12 are one-liners that remove actively
   misleading text.

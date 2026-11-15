# QA report — ingest, server, store

**Scope:** `ingest/`, `server/`, and the DuckDB store; the pseudonymisation contract
(`prd/02`), event shape (`prd/03`), and the Bitbucket DC export (`prd/04`).
**Code under test:** `700a177` (`state: playwright harness done; QA + corpus in flight`).
**Environment:** Python 3.14.10, DuckDB 2.0.0, FastAPI 0.163.1, uvicorn 0.54.0,
websockets 17.4; Bitbucket DC 9.4.18 lab at `http://127.0.0.1:7990` (`FIX/golden`).
**Method:** black-box plus white-box. Every ingest run used a throwaway store under
`/tmp/opencode/qa/` (`MEGASPEED_DB=…`, `MEGASPEED_IDENTITY=…`); `data/megaspeed.duckdb`
and the live server on `127.0.0.1:8766` were only ever read. Artifacts (adversarial
bundles, WS harness script, tamper stores) live in `/tmp/opencode/qa/` and are *not*
committed.

Nothing below is speculative: each bug has a command that reproduces it and a
measured expected-vs-actual. `GAP` entries are "works as built, but the built thing
is missing something the PRD needs".

---

## Bugs

### BUG-01 — Live Bitbucket reviewer changes are silently dropped (DC sends `UPDATED`, not `REVIEWER_UPDATED`) — **High**

**Where:** `ingest/pipeline.py:282` (`elif action == "REVIEWER_UPDATED"`),
`ingest/taxonomy.py:83` (`PR_ACTIVITY_MAP`), fixture generator
(`ingest/fixtures.py`, `add("REVIEWER_UPDATED", …)`).

**Repro:**

```bash
export MEGASPEED_BB_BASE_URL=http://127.0.0.1:7990 \
       MEGASPEED_BB_USER=admin MEGASPEED_BB_PASSWORD=admin-lab-pw
.venv/bin/python -m ingest --live --repo FIX/golden --probe
#   ignored   REVIEWED=1, UPDATED=8  (outside the closed vocabulary)
.venv/bin/python -m ingest --live --repo FIX/golden --probe --probe-bypass-filter
#   by activity {'comment_added': 23, 'pr_approved': 7, 'pr_declined': 5,
#                'pr_merged': 5, 'pr_opened': 10, 'pr_pushed': 4, 'pr_unapproved': 2}
curl -s -u admin:admin-lab-pw \
  'http://127.0.0.1:7990/rest/api/1.0/projects/FIX/repos/golden/pull-requests/1/activities?limit=200' \
  | jq -c '.values[] | select(.action=="UPDATED") | {action,addedReviewers,removedReviewers}'
# {"action":"UPDATED","addedReviewers":[{"name":"grace",…}],"removedReviewers":[]}
```

**Expected:** 8 `reviewer_changed` events. The activity `reviewer_changed` is in the
closed vocabulary (`prd/02`), the field is explicitly required by `prd/04` ("reviewer
changes"), and the store has an attribute spec for `change` in/out.

**Actual:** 0 `reviewer_changed` events for 8 real reviewer add/remove operations.
`--probe` reports them as `ignored (outside the closed vocabulary)`, so the loss is at
least visible, but the mapping table maps the *fixture-only* action name
`REVIEWER_UPDATED`; DC 9.4 emits `UPDATED` with `addedReviewers` / `removedReviewers`.
Because the fixture fabricates `REVIEWER_UPDATED`, `tests/` never exercises the real
shape (fixtures show `reviewer_changed=12`, live shows 0) — so the suite is green while
the reviewer dimension is empty in every live run.

**Impact:** no reviewer roster, no reviewer churn, no "reviewer added after N days",
and no way to attribute approvals to an assignment. `prd/04` lists "reviewer changes"
as part of the PR activity stream it considers "the most important data"; in live data
that signal is simply absent.

---

### BUG-02 — Incremental ingest never recomputes derived attributes; stale values persist forever — **High**

**Where:** `ingest/store.py:81` (`INSERT OR IGNORE`), `ingest/pipeline.py:131-160`
(`_event`), `ingest/pipeline.py:319-333` (`_approvals_lost_after` look-ahead).

**Repro:** ingest a bundle whose PR has `OPENED` + `RESCOPED`, then re-ingest the same
bundle plus the `UNAPPROVED` that the push caused (same natural keys, new activity id):

```bash
MEGASPEED_DB=/tmp/opencode/qa/inc2.duckdb MEGASPEED_IDENTITY=/tmp/opencode/qa/id-inc2.json \
  .venv/bin/python -m ingest --fixture-file /tmp/opencode/qa/inc-v1.json --repo QA/qa
MEGASPEED_DB=/tmp/opencode/qa/inc2.duckdb MEGASPEED_IDENTITY=/tmp/opencode/qa/id-inc2.json \
  .venv/bin/python -m ingest --fixture-file /tmp/opencode/qa/inc-v2.json --repo QA/qa
# select event_key, activity, attributes from events order by ts
# QA/qa#20:act:20002  pr_pushed  {"approvals_lost":false,…}   ← written by run 1
# QA/qa#20:act:20003  pr_unapproved  {"cause":"push"}         ← written by run 2
```

**Expected:** after run 2, `approvals_lost` on the `pr_pushed` event is `true` (the push
did cost an approval), or the event is versioned so both views exist.

**Actual:** `approvals_lost` stays `false`. `INSERT OR IGNORE` on `event_key` means a
row is written once and never revisited, but several attributes are computed with
run-local look-ahead or run-local counters (`approvals_lost`, `review_rounds`,
`replies`, `commits_added/removed`). Any fact that arrives in a later ingest is
permanently invisible in the earlier event.

**Impact:** the headline finding the PRD asks for — "pure rebase lost approvals" — is
systematically under-reported on any corpus ingested incrementally (the normal case:
daily/probe runs, or a PR still open on the first run). It also makes
`classifier_version` meaningless as a re-derivation knob: bumping the classifier
cannot rewrite existing events (see GAP-06).

---

### BUG-03 — `--fixture-file` silently truncates a bundle at 50 PRs; `prs_seen` is off by one — **Medium**

**Where:** `ingest/__main__.py:145`
(`limit=args.n if source == "fixture" else None` — `--fixture-file` also sets
`source="fixture"`), `ingest/pipeline.py:368` (`if stats.prs_seen > limit`).

**Repro:**

```bash
.venv/bin/python -c "from ingest import fixtures; from pathlib import Path; \
  fixtures.write_bundle(Path('/tmp/opencode/qa/b60.json'), n=60)"   # 60 PRs in the file
MEGASPEED_DB=/tmp/opencode/qa/b60.duckdb MEGASPEED_IDENTITY=/tmp/opencode/qa/id-b60.json \
  .venv/bin/python -m ingest --fixture-file /tmp/opencode/qa/b60.json
# PRs 51 seen -> 43 kept, 7 dropped (no ticket key)
```

**Expected:** `60 seen -> … kept, … dropped`; the replay of a bundle is bounded by the
bundle, not by the generator default. Also `prs_kept + prs_dropped == prs_seen`.

**Actual:** only 50 PRs are examined (9 never read), and the summary says **51 seen**
because the limit check happens after `prs_seen += 1`. `43 + 7 = 50 ≠ 51`, so the
invariant asserted in `tests/test_contract.py:91` is violated on this path. There is no
warning that data was skipped. Passing `--n 60` "fixes" it, which is exactly the kind
of hidden coupling that bites later.

---

### BUG-04 — Ticket filter has false positives on technical tokens (`UTF-8`, `SHA-256`, `ISO-8601`) — **Medium**

**Where:** `ingest/config.py:18` (`\b[A-Z]+-\d+\b`), `ingest/config.py:22`
(`key_shape`), `ingest/pipeline.py:82-93` (`KeyFilter.keys`).

**Repro:** a 13-PR adversarial bundle whose PR #11 contains no Jira key at all, only
`UTF-8` in the title and `SHA-256` in the description
(`/tmp/opencode/qa/adversarial.json`):

```bash
MEGASPEED_DB=/tmp/opencode/qa/adv.duckdb MEGASPEED_IDENTITY=/tmp/opencode/qa/id-adv.json \
  .venv/bin/python -m ingest --fixture-file /tmp/opencode/qa/adversarial.json --repo QA/qa
# PRs 13 seen -> 9 kept, 4 dropped (no ticket key)
# select object_id, ticket_keys from objects where object_type='TICKET';
# ('TICKET:UTF-8',) ('TICKET:SHA-256',)   ← plus the 9 real keys
```

**Expected:** 8 kept, 5 dropped; `UTF-8` / `SHA-256` are not ticket keys.

**Actual:** PR #11 is kept and two bogus `TICKET` objects are created; the PR's
`ticket_keys` are `['SHA-256','UTF-8']`. The second gate does not help — `UTF-8`
matches `^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$` perfectly.

**Impact:** any PR whose prose mentions a standard/version token (`UTF-8`, `SHA-256`,
`AES-128`, `HTTP-2`, `ISO-8601`, `IPV-4`, …) is treated as ticket-linked, and the
`TICKET` dimension gets fake keys. With a permissive production regex this also
inflates the kept-PR count, i.e. it *weakens the whole point of the filter*. A project-key
allowlist (or a `--keys` file, or ignoring a small stop-list) is missing.

---

### BUG-05 — The pseudonymisation guard has blind spots, and one of its two gates is configurable — **Medium**

**Where:** `ingest/store.py:279-331` (`verify`), `ingest/config.py:79-80`
(`MEGASPEED_TICKET_REGEX` **and** `MEGASPEED_KEY_SHAPE` env overrides),
`ingest/config.toml` `[filter] key_shape`.

`verify()` is the only automated enforcement of the PRD claim "the schema *is* the
contract; attempting to insert free text is a build/test failure". It checks (a) table
and column allowlists, (b) "no string value contains whitespace", (c) `zzcanary`.
Against a copy of a valid store, these tampers were applied and `--verify` run:

| Tamper | `--verify` | Correct? |
| --- | --- | --- |
| `ALTER TABLE events ADD COLUMN description VARCHAR` | exit 1, `columns outside the allowlist` | ✅ |
| `UPDATE events SET actor='Lauri Nomme'` | exit 1, `contain whitespace` | ✅ |
| `UPDATE objects SET ticket_keys=['zzcanarydeadbeef']` | exit 1, `canary value(s) leaked` | ✅ |
| `CREATE TABLE raw_comments (body VARCHAR)` | exit 1, `undeclared table` | ✅ |
| `UPDATE events SET activity='pr_reviewed'` | exit 1, `outside vocabulary` | ✅ |
| `UPDATE events SET actor='LauriNomme'` (raw name, no space) | **exit 0** | ❌ |
| `UPDATE objects SET ticket_keys=['feature/interleaved']` (branch name) | **exit 0** | ❌ |
| `UPDATE objects SET state='MERGEDish'`, `message_class='fixTheParser'` | **exit 0** | ❌ |
| `UPDATE ingest_runs SET source='bitbucket-prod-account-jdoe'` | **exit 0** | ❌ |
| `UPDATE events SET classifier_version='v1-launched-by-jdoe'`, `repo='ENG/lauri-private-repo'` | **exit 0** | ❌ |
| `UPDATE events SET content_hash='SomeRawCommitMessageWithoutSpaces'` | **exit 0** | ❌ |
| `DELETE FROM events WHERE event_key = min(...)` (orphan `event_objects` rows) | **exit 0** | ❌ |

Worse, both filter gates can be relaxed by config/environment and `verify()` will still
bless the result. Against the **real** lab corpus:

```bash
MEGASPEED_TICKET_REGEX='\b(?:feature|hotfix|experiment)/[a-z]+\b' \
MEGASPEED_KEY_SHAPE='^[a-z]+/[a-z]+$' \
MEGASPEED_DB=/tmp/opencode/qa/live-loose.duckdb \
.venv/bin/python -m ingest --live --repo FIX/golden
# PRs 10 seen -> 10 kept, 0 dropped · key found in branch=10
# contract       OK — no free text, no canaries
# TICKET:feature/rewritten, TICKET:hotfix/critical, TICKET:experiment/squash, …
```

**Expected:** raw branch names never enter the store (`DEV.md §7`: "branch names are
read for the filter and discarded; only the matched keys survive"), and the
"non-configurable-in-spirit" second gate cannot be turned off and then declared OK.
At minimum `verify()` should re-assert `key_shape` over `objects.ticket_keys` and a
pseudonym shape over `actor`.

**Actual:** `MEGASPEED_KEY_SHAPE` is honoured by `load_config`, so a filter change (or a
sloppy production regex) puts branch names into the `ticket_keys` column verbatim,
`verify()` returns `ok: true`, and ingest exits 0. Also note the guard's core assumption
("prose has whitespace, identifiers do not") is false for camelCase names, emails,
branch slugs and URLs — exactly the things a leak would look like.

**Impact:** the contract is enforced against *shape of the container*, not against
*content class*. Anyone who edits `config.toml`'s `key_shape` (or exports the env var)
silently weakens the boundary, and the one check that is supposed to catch it says OK.

---

### BUG-06 — Bot / service accounts are pseudonymised and counted as humans — **Medium**

**Where:** `ingest/pipeline.py:121-126` (`_actor`), `ingest/pseudonym.py:30-42`
(`split_name`); no `bot`/`human` attribute exists in `ingest/taxonomy.py:63-80`, no
`bot:<name>` synthetic actor is produced.

**Repro:**

```bash
# live store ingested with the loosened filter from BUG-05 so the corpus is kept
.venv/bin/python -c "
import duckdb; c=duckdb.connect('/tmp/opencode/qa/live-loose.duckdb', read_only=True)
print(c.execute('select activity, actor, count(*) from events group by 1,2 order by 1').fetchall())"
# ('comment_added','BiBi',4), ('pr_declined','BiBi',4), …
cat /tmp/opencode/qa/identity-live-loose.json | jq '.actors["bb:bitbucket.system-user"]'
# {"display":"Bitbucket","pseudonym":"BiBi", …}   ← DC user type: SERVICE
```

**Expected:** the DC user object carries `"type": "SERVICE"` for
`bitbucket.system-user`; `prd/02` has a closed `bot | human` dimension and `prd/03`
reserves synthetic actors (`CI`, `bot:<name>`), precisely so bot waits are not charged
to people. The four auto-decline comments ("This pull request has been inactive for 7
weeks…") should not be `comment_added` by a human-shaped actor, and the four declines
should not look human-caused.

**Actual:** `BiBi` is a normal `PERSON` object, its 4 comments and 4 declines are
ordinary human activity, and `sentinel` is the only "not really a person" signal in the
store. In this corpus **4 of 5 declines are bot activity** — i.e. 80 % of the decline
signal is noise, and every latency metric that includes `BiBi` is polluted. `prd/04`
("Bot account set — to filter non-human actors before any latency metric") is not
implemented.

---

### BUG-07 — The read API is 100 % unavailable while ingest writes, and the 503 leaks local paths/PID/username — **Medium**

**Where:** `server/app.py:44-53` (`store_conn` opens a fresh read-only DuckDB
connection per request), `ingest/store.py:394-419` (ingest holds the write connection
for the whole load), `app/main.js:148-150` (the `changed` broadcast triggers `load()`).

**Repro:**

```bash
MEGASPEED_DB=/tmp/opencode/qa/conc.duckdb .venv/bin/python -m server --port 8798 &
# poll /api/health in a loop, then run ingest into the same file
MEGASPEED_DB=/tmp/opencode/qa/conc.duckdb .venv/bin/python -m ingest --fixture --n 50
# 38 of 40 requests returned 503
```

Actual 503 body:

```json
{"ok":false,"error":"IO Error: Could not set lock on file \"/tmp/opencode/qa/conc.duckdb\": Conflicting lock is held in /usr/bin/python3.14 (PID 3986590) by user lauri. …"}
```

**Expected:** reads keep working during ingest (staging table + swap, or a retry/backoff
in `store_conn`, or the server tolerating a transient lock and returning the last good
snapshot). At most a short retry, not a hard down. And an API error should not expose
the absolute filesystem path, the OS username, and the server PID.

**Actual:** every endpoint is 503 for the duration of the ingest (seconds for fixtures,
minutes for a real corpus), and the error string is DuckDB's raw message. Combined with
the app's behaviour — `changed` → `load()` 250 ms later → `health.ok === false` → it
paints the "no store / run ingest" empty state and does **not** retry — a routine ingest
leaves the UI blank until a manual reload.

---

### BUG-08 — `/api/eval` and `/api/screenshot` return 500 on malformed input, are CORS-wide, and write arbitrary paths — **Medium**

**Where:** `server/app.py:147-175` (`float(body.get("timeout") or 10)` without a guard,
`_json_body` result used with `.get`), `server/app.py:83-88`
(`allow_origins=["*"]`, `allow_methods=["*"]`), `server/app.py:169-174`
(`Path(out).write_bytes`).

**Repro (against a browser-less server; the failure happens in `float()` before the
bridge is touched, so it is browser-independent):**

```bash
curl -s -X POST localhost:8799/api/eval -H 'content-type: application/json' \
  -d '{"code":"1","timeout":"abc"}'        # → 500 Internal Server Error (plain text)
curl -s -X POST localhost:8799/api/eval -H 'content-type: application/json' -d '[1,2]'
                                            # → 500 Internal Server Error
curl -s -X POST localhost:8799/api/screenshot -H 'content-type: application/json' \
  -d '{"timeout":"x"}'                      # → 500 Internal Server Error
curl -s -D- -o /dev/null -X OPTIONS -H 'Origin: http://evil.example' \
  -H 'Access-Control-Request-Method: POST' localhost:8766/api/eval
# access-control-allow-origin: *   access-control-allow-methods: DELETE, GET, …, POST, PUT …
curl -s -X POST localhost:8766/api/screenshot -H 'content-type: application/json' \
  -d '{"selector":"#timeline","out":"relative-shot.png"}'
# {"ok":true,…,"out":"relative-shot.png"} and the file appears in the server's CWD
```

**Expected:** a bad `timeout`/body is a 4xx JSON error (`{"ok":false,"error":…}`), never
a 500 and never a bare `Internal Server Error`; the eval bridge is at least origin- or
token-gated; `out` is constrained (it is an arbitrary-path write with attacker-supplied
content, executed as the server user).

**Actual:** 500 with no JSON body (the stack trace goes to the server log only); CORS
`*` for every method; `out` is resolved relative to the server CWD (I wrote
`megaspeed/relative-shot.png` and deleted it afterwards). Server log, verbatim:
`app.py:155 ValueError: could not convert string to float: 'abc'`,
`app.py:152 AttributeError: 'list' object has no attribute 'get'`,
`app.py:166 ValueError: … 'x'` (screenshot). Any page the user visits can
therefore `POST /api/eval` (arbitrary JS in the attached browser, i.e. read the API
through the browser) or `POST /api/screenshot` with an `out` path. The harness is a
localhost dev tool, so this is a "don't ship it like this" bug rather than a remote
compromise — but `app/main.js` proves the eval bridge is live in the normal UI session.

*(The documented no-browser behaviour is correct: `{"ok":false,"error":"no browser
connected"}` with HTTP 200 for both endpoints. The bug is only the malformed-input path
and the CORS/file-write surface.)*

---

### BUG-09 — `GET /api/events?to=YYYY-MM-DD` drops the whole final day — **Low**

**Where:** `server/app.py:113-130` + `ingest/store.py:48-63` (`to_naive_utc("2026-10-30")`
= `2026-10-30T00:00:00`) and `ingest/store.py:157` (`ts <= ?`).

**Repro:**

```bash
# QA server on a copy of the same fixture store (identical data to :8766)
curl -s 'localhost:8799/api/events?from=2026-10-01&to=2026-10-30'          | jq length  # 201
curl -s 'localhost:8799/api/events?from=2026-10-01&to=2026-10-30T23:59:59' | jq length  # 209
curl -s 'localhost:8799/api/events?from=2026-10-30&to=2026-10-30'          | jq length  # 0
```

**Expected:** a date-only `to` is inclusive of that day (DEV.md documents exactly
`?from=2026-10-01&to=2026-10-31`), and a single-day range returns that day.

**Actual:** the 8 events on 2026-10-30 disappear; `from == to` (date form) returns
nothing. A UI that passes calendar days silently loses every range's last day.

---

### BUG-10 — `ingest_runs` provenance counters are wrong (`events_skipped` always 0, `events_written` = attempted) — **Low**

**Where:** `ingest/pipeline.py:401-415`, `ingest/store.py:137-145`; the second fixture
run prints `420 emitted, 0 new` while the row records `events_written=420,
events_skipped=0`.

**Expected:** `events_written = 0`, `events_skipped = 420` on the idempotent re-run
(the column exists for exactly this).

**Actual:** `events_written` stays 420 and `events_skipped` is hard-coded to 0, so the
provenance table cannot distinguish "first load" from "no-op re-run". Separately, a live
run where every PR is filtered out records `source_watermark = NULL` (the watermark is
only tracked for kept PRs, `pipeline.py:385-387`), so "newest PR seen" is lost on the one
path where it matters most.

---

### BUG-11 — Common live failures print an unhandled Python traceback — **Low**

**Where:** `ingest/__main__.py:127` (`transport.check()`), `ingest/transport.py:124,128`;
`BitbucketError` is never caught.

**Repro:**

```bash
MEGASPEED_BB_BASE_URL=http://127.0.0.1:7990 MEGASPEED_BB_USER=admin \
MEGASPEED_BB_PASSWORD=wrong .venv/bin/python -m ingest --live --repo FIX/golden --probe
# Traceback … ingest.transport.BitbucketError: 401  for /rest/api/1.0/application-properties
MEGASPEED_BB_BASE_URL=http://127.0.0.1:9 … --probe   # same, "Connection refused"
… --repo NOSUCH/repo --probe                          # same, "404"
# A bundle whose repos differ from config's repos:
.venv/bin/python -m ingest --fixture-file /tmp/opencode/qa/adversarial.json
# Traceback … BitbucketError: fixture has no repo ENG/platform
```

**Expected:** `configuration/auth error: HTTP 401 …` and a non-zero exit, in the style of
the existing `SystemExit` messages (`--live` without creds is clean).

**Actual:** a full traceback for the three most likely operator mistakes (typo'd repo,
bad password, DNS/port) and for a mismatched fixture bundle. Exit code is 1 either way,
so scripts are fine; humans get a stack trace.

---

### BUG-12 — `/api/eval` runs in *every* attached browser and the first answer wins — **Low**

**Where:** `server/harness.py:116-131` (`broadcast` then wait for the first `evalres`).

**Repro:** open two WS clients (`/tmp/opencode/qa/ws_test.py`; two
`websockets.connect` calls), then `POST /api/eval`:

```
hello a: {"event":"hello","app":"megaspeed","clients":1}
hello b: {"event":"hello","app":"megaspeed","clients":2}
a got: {'eval': 3, 'code': '1+1'}
b got: {'eval': 3, 'code': '1+1'}
api result: {'ok': True, 'data': 'from-b', 'error': None}
```

**Expected:** a way to target a specific client (query param / client id), or at least
"the answer came from *a* browser; here is which".

**Actual:** both browsers execute the code (side effects twice), and the result is
whichever response arrives first — with the persistent headless chromium from
`harness/` plus a developer's tab, screenshots (`selector`/viewport dependent) are a
coin flip. The protocol is documented as mosaic-compatible, so this may be deliberate,
but it is untested and surprising.

---

### BUG-13 — `/api/events` silently ignores unknown filters — **Low**

**Where:** `server/app.py:113-118` (only `from`, `to`, `limit` are declared).

**Repro:** `curl -s 'localhost:8799/api/events?repo=ENG/mobile' | jq length` → **420**
(same for `actor=LaNo`, `ticket=PROJ-101`, `activity=pr_merged`, `type=PR`).

**Expected:** either the filter works, or the request is rejected (422) so a client
cannot believe it filtered.

**Actual:** 200 with the full log. This is the reverse of a gap: it is actively
misleading, because `?limit` *is* honoured and a UI author will reasonably assume the
rest are too. (Feature absence itself is GAP-04.)

---

### BUG-14 — Vacuous success paths: `--n 0`/negative and an empty store pass every gate — **Low**

**Where:** `ingest/__main__.py:98-111,145`, `ingest/store.py:279-331`,
`ingest/pipeline.py:368`.

**Repro:**

```bash
MEGASPEED_DB=/tmp/opencode/qa/n0.duckdb MEGASPEED_IDENTITY=/tmp/opencode/qa/id0.json \
  .venv/bin/python -m ingest --fixture --n 0
# events 0 emitted, 0 new · contract OK — no free text, no canaries   (exit 0)
MEGASPEED_DB=/tmp/opencode/qa/n0.duckdb .venv/bin/python -m ingest --verify; echo $?   # 0
# negative: --n -5 → also 0 PRs, exit 0
```

**Expected:** `0 events` is a failure worth flagging (`prs_kept == 0` on a live run
already happens legitimately, so maybe a warning rather than exit 1), and `--verify` on
an empty store should at least say "nothing to verify".

**Actual:** both exit 0 and the contract line reads like a green check. In CI, a typo in
`--n` or a mis-set `MEGASPEED_DB` produces a green run against an empty store. Same
family: a live ingest of `FIX/golden` reports `0 events` and exits 0 (correct behaviour
for a keyless corpus, but indistinguishable from "the filter ate everything").

---

## Gaps

### GAP-01 — `push_class` is always `other` in live mode — **High impact**
No local clone and no `git patch-id`/`range-diff` (`prd/02`, `prd/04`). Fixtures inject
the label through a non-DC `megaspeedPatchId` block (`ingest/fixtures.py:6-12`), so the
fixture store shows a healthy mix (`content`/`pure_rebase`/`rebase_conflict`) and live
shows `{"push_class":"other"}` on every `pr_pushed`:
`.venv/bin/python -m ingest --live --repo FIX/golden --probe --probe-bypass-filter`
prints `pr_pushed … 'push_class': 'other'`. **Impact:** the rebase-waste analysis the
PRD exists for (and the Auto-Unapprove config finding) cannot be computed at all; a
`pure_rebase` that lost approvals is invisible. Any dashboard that colours pushes by
class would show one bucket in production.

### GAP-02 — `REVIEWED` / `UPDATED` activities, and review-thread state, are not modelled — **High impact**
`REVIEWED=1` and `UPDATED=8` are counted as `ignored` by `--probe` and dropped by the
reducer (minus BUG-01, where `UPDATED` should become `reviewer_changed`). Nothing emits
`pr_reviewed`, and there is no `ReviewThread` object (`prd/03`) with
`severity/state/resolved_by`: the pipeline reduces comments to a *snapshot*
(`thread_state`, `severity`, `anchor_class`, `replies`) and discards
`comment.resolvedDate` / `comment.resolver` / `threadResolved`, which real DC payloads
carry. **Impact:** no review latency (comment → resolution), no resolved-by, no
per-reviewer thread stats; `prd/03`'s `ReviewThread` table is absent from the store.

### GAP-03 — Timezone / business-hours / `as_of` handling is thin — **High impact**
- Store timestamps are naive UTC (`store.py:44-67`) and the API emits `Z` — consistent,
  but there is **no timezone metadata anywhere** (not on `Person`, not on
  `ingest_runs`, not in `/api/health`), and `prd/03`'s "working-hours calendar" object
  does not exist. `app/timeline.js:367-385` therefore hard-codes weekend shading plus an
  `18:00–08:00 UTC` "night" band.
- Every wait is therefore wall-clock, not business hours (`prd/03`: "Without it,
  weekends dominate every number").
- `as_of` is stored per event and per run, and `summary()` reports the latest run's
  `as_of`, but **no endpoint accepts `as_of`/`since`**; events are immutable
  (`INSERT OR IGNORE`) while `objects` are overwritten (`INSERT OR REPLACE`,
  `store.py:89-93`) with **no `as_of` column at all** — so "what did we know on day T"
  cannot be answered for PR state/closure/head commit, and a partial run (e.g.
  `--n 25`) still stamps the whole store with a full-snapshot `as_of`, which the UI uses
  for right-censoring (`app/main.js:29`). `iso()` truncates to whole seconds, so runs in
  the same second are indistinguishable.
- `DEV.md §7` says "delete `var/identity_map.json` and re-ingest to rebuild". That does
  not rebuild: existing `events`/`objects.actor` keep the old pseudonyms while new rows
  get new ones (collisions are allocated in first-seen order,
  `ingest/pseudonym.py:86-98`), so one person can become two actors inside one store.

### GAP-04 — No repo / actor / ticket / activity filtering, pagination or aggregation in the API — **Medium impact**
`/api/events` takes only `from`, `to`, `limit` (earliest-first; no `offset`, no `order`,
no total count). The UI fetches **all** events + **all** objects on every `changed`
broadcast (`app/main.js:91-119`). Measured: the fixture's 420 events serialise to
≈ 111 kB; a 42 000-event store returns all 42 000 rows in a single payload
(`list_events` 0.62 s, 11.1 MB of JSON). **Impact:**
no server-side swimlane pre-filtering or drill-down, no "zoom into this week without
reloading everything", no density pre-aggregation; the browser becomes the bottleneck at
exactly the corpus size (100 tickets × 3 months) the project is targeting.

### GAP-05 — Most `prd/03` object types and several DC exports are absent — **Medium impact**
Store has only `PR`, `COMMIT`, `TICKET`, `PERSON`. Missing: `PRRevision` (no push_index,
patch-id, push_class per rescope — the timeline cannot draw revision boundaries),
`ReviewThread`, `Story`/`Epic`/`Sprint`/`OpenQuestion`, `Person.role_class` and the
calendar. Also missing from the Bitbucket export: target-branch relationship
(`toRef.displayId` is discarded, and the `stacked` attribute in
`taxonomy.py:74` is never emitted → no stacked-PR DAG), the merge commit
(`MERGED.commit`), per-revision diff stats, build status / CI actor (the `build_status`
activity is in the vocabulary but never emitted), and sentinel *membership* history
(only a per-approval boolean `sentinel`). **Impact:** the dependency graph, the queue/"who
blocks whom" views (`prd/05`, `prd/09` views 2–4) and the gantt's revision detail cannot
be built from the store; they need re-ingestion, not a UI change.

### GAP-06 — No question / phase classification, and no re-classification path — **High impact**
`question_raised`, `question_answered` and the whole `phase` vocabulary
(`breakdown/refinement/sprint/implementation/review`) are declared and never emitted;
there is no local classifier, no OpenQuestion object, and no attribution of
`comment_added` severity beyond `comment.severity == "BLOCKER"` (i.e. no
BLOCKER-vs-suggestion inference when DC has no severity). **Impact:** the PRD's central
"questions live outside any ticket" thesis is unimplemented; `severity` is effectively
"did the reviewer tick BLOCKER", and the analysis layer has no phase dimension to
bucket by. Related: because ingest is `INSERT OR IGNORE` (BUG-02) and there is no
`--reclassify`, bumping `classifier_version` can never rewrite existing rows, so the
reproducibility story (`prd/02` §data lifecycle, "record `classifier_version` … so
outputs are reproducible/tunable") only works into a fresh store.

### GAP-07 — Only Bitbucket is ingested; no Jira/Tempo/Slack, so the object-centric log is PR-only — **Medium impact**
`ingest/` contains one source. No issue snapshot/changelog/sprints/worklogs, no Slack
linkage, no identity resolution across sources (`prd/03` identity table is a
Bitbucket-slug ↔ pseudonym map only). **Impact:** no `created`/`assigned`/`pointed`/
`sprint_*`/`worklogged` events, no lead time before the first PR, no refinement batching,
no carry-over; the Jira half of every process question is missing. The PR-side data is
good enough to prove the pipeline, not to answer the questions in `prd/01`.

### GAP-08 — Fixtures do not resemble DC 9.4 closely enough to catch production bugs — **Medium impact**
Concrete divergences found while testing: reviewer changes use the invented
`REVIEWER_UPDATED` action (BUG-01); `megaspeedPatchId` is a non-DC block (GAP-01); no
`REVIEWED`/`UPDATED`/comment-edit/delete activities; no `SERVICE`/bot users (BUG-06); no
non-ASCII names (Estonian `õ/ä/ö/ü` are common in the target org, and
`pseudonym._NON_ALPHA` strips them, e.g. `Mari Müür → MaMr`); the key placement is
always a single field (`keys_by_placement` sums exactly to `prs_kept`, so multi-placement
counting is untested); all `replies` are 0 or 1 with no resolved threads. **Impact:** the
13 contract tests pass on a corpus that cannot reproduce the two highest-impact live
bugs above.

### GAP-09 — Test coverage stops at the store — **Low impact**
`tests/test_contract.py` covers pseudonyms, the filter's second gate, message
classification, idempotency, canaries and the allowlist. Not covered: the API
(`/api/events` bounds/ordering, 503 paths), the WS bridge, `--fixture-file` limiting
(BUG-03), incremental staleness (BUG-02), `verify()` blind spots (BUG-05), the live
transport, and any DC 9.4 payload shape. The `--probe` "ignored" counters are the only
alarm for taxonomy drift, and nothing asserts they are zero.

### GAP-10 — Docs drift from behaviour — **Low impact**
`DEV.md §2` says `--probe --probe-bypass-filter` "maps 48 events" — actual **56** on
`FIX/golden`. `DEV.md §2` presents `--fixture-file` as "replay a committed bundle" —
it is silently capped at 50 PRs (BUG-03). `DEV.md §7` claims deleting
`var/identity_map.json` + re-ingest rebuilds pseudonyms — it does not (GAP-03).
`DEV.md §3` says `key_shape` is "non-configurable-in-spirit" while `config.toml` and
`MEGASPEED_KEY_SHAPE` both expose it (BUG-05). `DEV.md §4` promises a clean
`{"ok":false,"error":…}` for the harness without a browser (true) but says nothing about
malformed bodies, which 500 (BUG-08).

### GAP-11 — UI data-contract mismatches (app-side, surfaced while checking API sufficiency) — **Low impact**
`/api/events` + `/api/objects` + `/api/health` do carry what the current renderer needs
(timestamps, activity labels, pseudonyms, PR `created_at`/`closed_at`/`state`, ticket
keys, attributes). Two things the UI assumes are not guaranteed by the data:
`app/timeline.js:282-287` builds the PR swimlane key as `#${event.pr_id}` **without the
repo**, but PR ids are per-repo — the fixture store alone contains
`PR:ENG/platform#100` *and* `PR:ENG/mobile#100`, so two distinct PRs collapse into one
lane (the API does return `repo`, so this is a one-line app fix). And the lifecycle
segments/phase colours are re-derived client-side from activity order
(`phaseOf`), with "waiting on author" hard-coded; there is no server-provided
phase/handover signal to agree or disagree with (`prd/05`).

---

## Verified OK

For the record — these were tested and behave as designed (so they should not be
"fixed" while addressing the above):

| Check | Result |
| --- | --- |
| Fixture ingest, `--n 50` | `50 seen → 43 kept, 7 dropped`; `420 events`, `182 commits`, `248 objects`, 8 pseudonyms; exit 0; matches `DEV.md` exactly |
| `--n 1 / 7 / 50 / 200` | 5 / 54 / 420 / 1574 events, all `contract OK`, deterministic (seed 20261115) |
| Idempotency, same temp DB twice | second run `420 emitted, 0 new`; no duplicate `event_key`, no duplicate `event_objects`, `objects` row count unchanged |
| Ticket-key filter placement | keys found only in branch / only in title / only in description / only in commit message are each kept; a PR with no key anywhere is dropped (adversarial bundle: 13 seen → 9 kept, 4 dropped) |
| Second gate (`key_shape`) | drops `WHATEVER-1234567` (7 digits), `proj-1` (lowercase), `TOOLONGPROJECT-12` (13-char project) |
| Canary | `zzcanary…` appears in every raw fixture field; **0** occurrences in the `.duckdb` bytes of every test store and **0** in `/api/health` + `/api/events` + `/api/objects` responses (both servers) |
| Raw text in live store | borrowed prose from the lab (titles/descriptions/comments) searched in the store file: `sanitization`, `pagination`, `inactive`, `resetting`, `force-pushed`, `cleanups`, `dependent`, `screen` → 0 hits |
| Pseudonyms | `lauri.nomme → LaNo`, `AlSm`, `BiBi`…; identity map is a separate raw-zone file, never served, not written by `--probe` |
| `--verify` exit codes | 0 on a good store, 1 with the offending table/column/value printed, 2 when the store is missing |
| Store shape | 4 allowlisted tables only; no free-text columns; 0 events without `ts`/`activity`/`actor`; 0 events without objects; 0 dangling `event_objects`; 0 events past `now()`; every `ticket_keys` value matches `key_shape`; `message_class` ∈ vocabulary; `classifier_version` constant |
| Live `--probe` | 21 GETs, 10 PRs / 18 commits, `0 matched`, ignored counters printed; nothing written to the lab (PR list identical before/after: same ids, states, `updatedDate`, `version`), no identity file created |
| Live `--live --repo FIX/golden` | `10 seen → 0 kept`, 0 events, exit 0, empty store that verifies — i.e. the keyless corpus is dropped and the client is genuinely read-only (only `method="GET"` exists in `ingest/transport.py`) |
| API read paths | `/api/health` (200 + counts/provenance), `/api/events` (420 rows, globally `ts ASC` stable, unique keys, exact field set, `Z` timestamps, objects+attributes present), `/api/objects` (43 PR / 182 COMMIT / 15 TICKET / 8 PERSON), `limit=3` works, `limit=0|200001|abc` → 422, bad `from` → 400 JSON, `from`/`to` with `+03:00` offsets converted correctly, missing store → 503 + hint on all three endpoints, static `/` → 200 |
| WebSocket | 8/8: `{"event":"hello"}` on connect (with `clients` count), `ping`→`pong` echo, `{"eval":id,"code":…}` request, `evalres` round-trip, chunked `evalchunk` reassembly above the threshold, `{"event":"changed"}` broadcast on store mtime change, connection survives a non-JSON frame, `browsers` count returns to 0 on disconnect |
| `/api/eval` + `/api/screenshot` with **no** browser | both `{"ok":false,"error":"no browser connected"}`, HTTP 200, no traceback/log noise |
| Screenshot with a browser | `{"ok":true,…,"bytes":33858,"mime":"image/png"}`, a valid 1600×969 PNG |
| `python -m unittest discover -s tests` | 13/13 pass |
| Read-only discipline of this QA pass | `data/megaspeed.duckdb` only read; the server on `:8766` was never killed or written; all ingest/patch experiments used `/tmp/opencode/qa/*.duckdb` |

---

## Suggested fix order (for the follow-up build pass)

1. **BUG-01** (reviewer changes) — one mapping in `pipeline.py` + a fixture that uses the
   DC 9.4 `UPDATED` shape, otherwise the reviewer dimension stays empty in production.
2. **BUG-02 + GAP-06** (immutable derived attributes / no re-classification) — needs a
   decision: version events (`event_key` + `run_id`) or an UPDATE/rebuild path keyed by
   `classifier_version`. Everything downstream inherits the choice.
3. **BUG-04 + BUG-05** (key false positives, configurable second gate, `verify()` blind
   spots) — cheap, and they protect the load-bearing wall.
4. **BUG-03, BUG-06, BUG-07** — operator-facing correctness/availability.
5. **BUG-08, BUG-09, BUG-10, BUG-13, BUG-14** — small, self-contained.
6. Then the object-model gaps (GAP-01, GAP-05, GAP-04) as the next slice, driven by which
   of the PRD's views is built first.

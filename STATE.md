# STATE — megaspeed progress & open items

## Done

- [x] Repo root, PRD + UX spec, deploy (nginx+DNS), walking skeleton, Playwright harness, both QA reports.
- [x] **Corpus** — `bb-lab-b` licensed + `DEL` corpus (131 PRs, 100 tickets, 5 users + admin, ~3 months).
- [x] **Ingest-fix** — 63 tests green; real DC `UPDATED`→`reviewer_changed`, incremental recompute, prefix
      filter, bots, tighter `--verify`, `PRREV`/`THREAD` objects.
- [x] **Fix + build UI** — 14 UI bugs fixed; blocking map + queue + delivery + overview + sources +
      policy-stub; shell (sidebar, scope/timeframe, scope banner); `analysis/ballcourt.py` → `/api/blocking`,
      `/api/queues`, `/api/delivery`.
- [x] **Wire + verify ingest vs `DEL`** — 131 seen → 131 kept / 0 dropped; 1316 events; `reviewer_changed`
      **266** (was 0); bots classified (14 bot events); 0 false-positive keys; `--verify` `contract OK`;
      idempotent re-run (`0 new`); incremental-recompute proven (`approvals_lost` flips on 12 re-derived);
      byte-level audit: no raw prose/whitespace anywhere.

## In progress (fallback `opencode-go/deepseek-v4.1-flash`)

- [ ] **Server-fix** (`server/` + `app/`): eval/screenshot 400s, CORS, `screenshot.out` path restriction,
      date-only `to=` inclusive, unknown filters, graceful lock 503, pagination/aggregation.

## Next (dispatch on completion)

1. **Final integration** — restart 8766 on the new code, ingest `DEL` into the live store
   (`MEGASPEED_TICKET_PROJECTS=DEL`), point the UI at real data, smoke-test every view via the harness.
2. **Docs drift** — `docs/DEV.md` + `ingest/config.toml` still say port 7990 and "REVIEWED/UPDATED ignored".

## Known (corpus / deferred — not bugs in the ingest)

- `reviewer_changed` has no `removed` cases (DEL corpus never emits `removedReviewers`); `REVIEWED` folds
  into `reviewer_changed{change=reviewed}` (a future `pr_reviewed` label would be cleaner).
- 6 human actors (incl. `bb:admin`→`LaAd`, the seeding account) — not exactly 5.
- PERSON object links partial by design (only `pr_approved`/`reviewer_changed` link PERSON; read `events.actor`).
- 265/467 COMMIT objects unlinked (no per-commit event); `merge_commit` absent (DEL `MERGED` has no commit block).
- THREADs all `state=open` (corpus has no thread resolution) — resolution path untested live.
- `push_class` = `other` on all live pushes (needs git patch-id clone — GAP-01).
- `sentinel=false` on all approvals (no sentinel slugs in DEL) — sentinel dimension inert here.

## Follow-ups (later slices)

- Light theme; selection/pivot on graph; commits+tickets on timeline (G13); semantic zoom (G2);
  cost-labelled load (G14); business-hours calendar; queue WIP sweep-line.
- `push_class` via patch-id; question/phase LLM classification; Story/Epic/Sprint (Jira+Tempo).

## Test lab (Bitbucket DC 9.4.18)

- `bb-lab-a` :7990 — FIX/golden (no keys). `bb-lab-b` :7991 — LICENSED, `DEL` (env override needed:
  `MEGASPEED_BB_BASE_URL=http://127.0.0.1:7991`, `MEGASPEED_BB_USER=admin`, `MEGASPEED_BB_PASSWORD=admin-lab-pw`).
- Users `admin`/`admin-lab-pw`, alice/bob/carol/dave/erin (`lab-pw-<name>`).

## Open items (real production data)

- [ ] Production Bitbucket DC URL + auth. [ ] Jira ticket-key format.

## Subagent model

- Primary: `opencode/deepseek-v4.1-flash-free` (exhausted) → Fallback: `opencode-go/deepseek-v4.1-flash`

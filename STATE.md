# STATE — megaspeed progress & open items

## Done

- [x] Repo root moved to `megaspeed/`; history retained.
- [x] PRD (`prd/01..09`) + UX spec (`prd/10-ux-look-feel.md`).
- [x] `PLAN.md`, `STATE.md`, `megaspeed.example.com`.
- [x] **Deploy live**: nginx + DNS — `megaspeed.example.com → 203.0.113.10`, upstream `127.0.0.1:8766`.
- [x] **bb-lab-a** resurrected (FIX/golden, no ticket keys).
- [x] **Walking skeleton** (ingest + server + app + 13 tests), verified end-to-end.
- [x] **Playwright + browser harness** (`harness/`), eval + screenshot verified.
- [x] **QA timeline UI** → `docs/QA-UI-REPORT.md` (14 bugs, 14 gaps).
- [x] **QA ingest/server/store** → `docs/QA-REPORT.md` (8 bugs incl. 2 HIGH, + gaps).

## In progress (subagents, fallback `opencode-go/deepseek-v4.1-flash`)

- [ ] **Fix + build UI** (`app/` + `server/`): fix HIGH/MED timeline bugs; build blocking map,
      queue view, delivery list; add sidebar/top-bar. Tests on a separate port 8767.
- [ ] **Fix ingest** (`ingest/` + `fixtures/` + `tests/`): real DC `UPDATED`→reviewer_changed,
      incremental recompute, ticket-key false positives, bots, tighter `--verify`, fixture off-by-one.
- [ ] **Corpus** — resurrect `bb-lab-b` (licensed) + generate ~100-ticket / 5-person / ~3-month `DEL-*` corpus.

## Next (dispatch on completion)

1. **Server-fix pass** (after UI + ingest fixes land, to avoid `server/` conflict): 503 lock during
   ingest, `/api/eval`+`/api/screenshot` 500 on bad input, CORS, arbitrary `screenshot.out` path,
   date-only `to=` dropping the final day, filters/pagination/aggregation for the UI.
2. **Wire real ingest** against `bb-lab-b` `DEL-*` corpus (configure `MEGASPEED_TICKET_PROJECTS=DEL`),
   verify ticket-key ingest end-to-end.
3. Update `STATE.md` after each.

## Deferred (known, later slices)

- `push_class` via git patch-id (needs a local clone).
- question/phase LLM classification (local cheap LLM, prd/02).
- timezone / business-hours calendar / `as_of` query.
- Story/Epic/Sprint objects (needs Jira Cloud + Tempo ingest).
- SvelteKit shell (current UI is vanilla + canvas; renderer is shell-agnostic).

## Test lab (Bitbucket DC 9.4.18)

- `bb-lab-a` :7990 (`admin`/`admin-lab-pw`) — FIX/golden (no keys).
- `bb-lab-b` :7991 — being resurrected + licensed by the corpus subagent.
- Scripts: `~/dev/hans/lab/` (`lab-up.sh`, `wizard.sh`, `lab-down.sh`, `config.env`, `SEED.md`).

## Open items (real production data)

- [ ] Production Bitbucket DC URL + project/repo(s) + auth.
- [ ] Jira ticket-key format for production.

## Subagent model

- Primary: `opencode/deepseek-v4.1-flash-free` (exhausted → fallback)
- Fallback: `opencode-go/deepseek-v4.1-flash`

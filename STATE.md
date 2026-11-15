# STATE — megaspeed progress & open items

## Done

- [x] Repo root moved to `megaspeed/`; history retained.
- [x] PRD (`prd/01..09`) + UX spec (`prd/10-ux-look-feel.md`).
- [x] `PLAN.md`, `STATE.md`, `megaspeed.example.com`.
- [x] **Deploy live**: nginx + DNS — `megaspeed.example.com → 203.0.113.10`, upstream `127.0.0.1:8766`.
- [x] **bb-lab-a** resurrected (FIX/golden, no ticket keys).
- [x] **Walking skeleton** (ingest + server + app + 13 tests), verified end-to-end.
- [x] **Playwright + browser harness** (`harness/`), eval + screenshot verified.
- [x] **QA timeline UI** → `docs/QA-UI-REPORT.md`.
- [x] **QA ingest/server/store** → `docs/QA-REPORT.md`.
- [x] **Corpus** — `bb-lab-b` resurrected + LICENSED (COMMERCIAL DC, push acid test passed), and the
      `DEL` corpus generated (see below).

## Corpus (bb-lab-b, `DEL`)

- `bb-lab-b` :7991 (`admin`/`admin-lab-pw`); 5 users alice/bob/carol/dave/erin (pw `lab-pw-<name>`).
- `DEL` → `delivery-api` (79 PRs) + `delivery-web` (52 PRs). **131 PRs**: 107 merged / 14 open / 10 declined.
- 100 tickets `DEL-100`…`DEL-199`, epic `DEL-1` (in every PR description).
- Realistic activity: OPENED 131, COMMENTED 333, RESCOPED 202, APPROVED 241, UNAPPROVED 12, REVIEWED 4, MERGED 107, DECLINED 10; 12 stacked PRs; sentinel alice + rotating reviewer.
- Time: commits 2026-08-13 → 2026-11-12 (576 commits), PR events → 2026-11-13.
- Scripts in `corpus/` (generate.py, backdate.py, verify.py, README.md, state.md). Note: PR/comment/approve
  timestamps were backdated via an H2-only trick (unsupported on Postgres); pre-edit DB backup at
  `corpus/build/backdate/db.backup.mv.db`.
- Filter contract verified: 131/131 matched (all PRs keyed — so **negative/reject cases come from
  bb-lab-a's FIX/golden and the fixture corpus**, not from DEL).
- License is a rolling ~3h dev window; read-only ingest works even after expiry, pushes don't. Renew via
  `hans/lab/license.txt` (same key) or re-run lab-up.

## In progress (subagents, fallback `opencode-go/deepseek-v4.1-flash`)

- [ ] **Fix + build UI** (`app/` + `server/`): timeline bug fixes + blocking map/queue/delivery views + sidebar.
- [ ] **Fix ingest** (`ingest/` + `fixtures/` + `tests/`): real DC `UPDATED`→reviewer_changed, incremental
      recompute, ticket-key false positives, bots, tighter `--verify`, fixture off-by-one.

## Next (dispatch on completion)

1. **Wire real ingest** (after ingest-fix): run the FIXED ingest against `DEL` (`MEGASPEED_TICKET_PROJECTS=DEL`),
   verify 131 PRs → all kept, reviewer_changed now populated, bots classified, no false positives, `--verify` green.
2. **Server-fix pass** (after UI + ingest fixes, to avoid `server/` conflict): 503 lock during ingest,
   `/api/eval`+`/api/screenshot` 500s, CORS, arbitrary `screenshot.out` path, date-only `to=` bug,
   filters/pagination/aggregation.
3. Update `STATE.md` after each.

## Deferred (known, later slices)

- `push_class` via git patch-id (needs a local clone).
- question/phase LLM classification (local cheap LLM, prd/02).
- timezone / business-hours calendar / `as_of` query.
- Story/Epic/Sprint objects (needs Jira Cloud + Tempo ingest).
- SvelteKit shell (current UI is vanilla + canvas).

## Test lab (Bitbucket DC 9.4.18)

- `bb-lab-a` :7990 — FIX/golden (force-push/rescope corpus, **no keys** → negative filter cases).
- `bb-lab-b` :7991 — LICENSED, DEL corpus (positive filter cases).
- Scripts: `~/dev/hans/lab/` (`lab-up.sh`, `wizard.sh`, `lab-down.sh`, `config.env`, `SEED.md`).

## Open items (real production data)

- [ ] Production Bitbucket DC URL + project/repo(s) + auth.
- [ ] Jira ticket-key format for production.

## Subagent model

- Primary: `opencode/deepseek-v4.1-flash-free` (exhausted → fallback)
- Fallback: `opencode-go/deepseek-v4.1-flash`

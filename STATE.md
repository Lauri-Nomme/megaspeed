# STATE — megaspeed progress & open items

## Done

- [x] Repo root moved to `megaspeed/`; history retained.
- [x] PRD (`prd/01..09`) + UX spec (`prd/10-ux-look-feel.md`).
- [x] `PLAN.md`, `STATE.md`, `megaspeed.example.com`.
- [x] **Deploy live**: nginx + DNS — `megaspeed.example.com → 203.0.113.10`, upstream `127.0.0.1:8766`.
- [x] **bb-lab-a** resurrected (FIX/golden corpus, no ticket keys).
- [x] **Walking skeleton** (ingest + server + app + 13 tests), verified end-to-end:
  - `https://megaspeed.example.com/` 200; `/api/health`, `/api/events`, `/ws`, `/api/eval`, `/api/screenshot`.
  - Fixture: 50 PRs → 43 kept (ticket keys), 420 events, 182 commits. Store contract `--verify` green.
  - Live FIX/golden: 10 PRs → 0 kept (no keys) — filter correct.
- [x] **Playwright + browser harness** (`harness/`): playwright 1.62.0 (chromium-1234, cached), `browser.py` launcher, eval + screenshot verified (`harness/proof.png`).

## In progress (subagents, fallback model `opencode-go/deepseek-v4.1-flash`)

- [ ] **QA ingest/server/store** → `docs/QA-REPORT.md` (read-only; temp DBs).
- [ ] **QA timeline UI** (screenshot pipe) → `docs/QA-UI-REPORT.md`.
- [ ] **Corpus** — resurrect `bb-lab-b` (licensed) + generate ~100-ticket / 5-person / ~3-month `DEL-*` corpus.

## Next (dispatch on completion)

1. **Fix + build pass** — read both QA reports, fix bugs, and build the missing views
   (blocking map, queue view, delivery list) + backend ball-in-court / handover-graph
   / queue computation (prd/05-analysis.md, prd/09-web-ui.md views 2–4).
2. **Wire real ingest** against `bb-lab-b` `DEL-*` corpus (set filter `DEL-\d+`), verify ticket-key ingest end-to-end.
3. Update STATE.md after each.

## Deviations / notes

- No SvelteKit yet — vanilla ES modules + canvas served by FastAPI (renderer is shell-agnostic).
- `push_class` = `other` in live mode (no local git clone / `git patch-id` yet).
- `REVIEWED`/`UPDATED` DC activities ignored by closed vocab; `--probe` surfaces them.
- A real browser tab is already attached via `megaspeed.example.com` (health reports 2 browsers).

## Test lab (Bitbucket DC 9.4.18)

- `bb-lab-a` :7990 (`admin`/`admin-lab-pw`) — FIX/golden (force-push/rescope corpus, no keys).
- `bb-lab-b` :7991 — being resurrected + licensed by the corpus subagent.
- Scripts: `~/dev/hans/lab/` (`lab-up.sh`, `wizard.sh`, `lab-down.sh`, `config.env`, `SEED.md`).

## Open items (real production data)

- [ ] Production Bitbucket DC URL + project/repo(s) + auth.
- [ ] Jira ticket-key format for production.

## Subagent model

- Primary: `opencode/deepseek-v4.1-flash-free` (exhausted → fallback)
- Fallback: `opencode-go/deepseek-v4.1-flash`

# STATE — megaspeed progress & open items

## Done

- [x] Repo root moved to `megaspeed/` (was `megaspeed/prd/`); history retained.
- [x] PRD complete (`prd/01..09`) + UX spec (`prd/10-ux-look-feel.md`).
- [x] `PLAN.md`, `STATE.md`, `megaspeed.example.com`.
- [x] **Deploy live**: nginx + DNS — `megaspeed.example.com → 203.0.113.10`, WebSocket
      headers, upstream `127.0.0.1:8766`. Verified 200 through nginx.
- [x] **bb-lab-a** resurrected (FIX/golden corpus; no Jira ticket keys).
- [x] **Walking skeleton** (subagent) — ingest + server + app + tests, verified
      end-to-end:
  - Live at `https://megaspeed.example.com/` (HTTP 200), `/api/health`, `/api/events`, `/ws`.
  - Fixture corpus: 50 PRs → 43 kept (ticket keys `ARCH-*`, `PROJ-*`, …), 420 events, 182 commits.
  - Store: DuckDB, no free-text columns, canary tokens, `--verify` contract gate, 13 tests.
  - Live lab (FIX/golden): 10 PRs → 48 events, **0 kept** (no keys) — filter working.
  - Server runs via `nohup .venv/bin/python -m server` (pid varies; restart with `fuser -k 8766/tcp`).

## In progress

- [ ] **Corpus generation** (subagent): resurrect `bb-lab-b` as licensed + generate a
      ~100-ticket, 5-person, ~3-month PR corpus with `DEL-*` ticket keys, into
      `bb-lab-b` (:7991).

## Deviations / notes (walking skeleton)

- No SvelteKit yet — vanilla ES modules + canvas served by FastAPI (renderer can move
  into a SvelteKit shell later unchanged).
- `push_class` = `other` in live mode (no local git clone / `git patch-id` yet);
  fixtures simulate it via a namespaced field.
- `REVIEWED` / `UPDATED` DC activities are ignored by the closed vocabulary, but
  `--probe` counts them so taxonomy gaps stay visible.
- Screenshots need a browser connected to `/ws` (health reports `browsers: 0` now).
  Playwright chromium is cached at `~/.cache/ms-playwright` (`chromium-1234`,
  `chromium_headless_shell-1234`), but the `playwright` python package is not yet
  installed.

## Test lab (Bitbucket Data Center 9.4.18)

- `bb-lab-a` (`sudo nerdctl`), `atlassian/bitbucket:9.4.18-jdk21`.
  - HTTP `http://127.0.0.1:7990`, sysadmin `admin` / `admin-lab-pw`.
  - Project `FIX` → repo `golden` (force-push/rescope/stacked/declined corpus, **no ticket keys**).
- `bb-lab-b` (HTTP :7991, SSH :7998) — being resurrected+licensed by the corpus subagent.
- Lab scripts: `~/dev/hans/lab/` (`lab-up.sh`, `wizard.sh`, `lab-down.sh`, `config.env`,
  `SEED.md`); license `lab/license.txt` (+ `trial3h.txt`).

## Open items (blockers for real production data)

- [ ] Production Bitbucket DC base URL + project/repo(s) + auth.
- [ ] Jira ticket-key exact format for production + which projects to scope.

## Subagent model

- Primary: `opencode/deepseek-v4.1-flash-free`
- Fallback: `opencode-go/deepseek-v4.1-flash`

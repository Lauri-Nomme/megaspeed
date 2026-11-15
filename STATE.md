# STATE — megaspeed progress & open items

## Done

- [x] Repo root moved to `megaspeed/` (was `megaspeed/prd/`); full history retained.
- [x] PRD complete: `prd/01..09`.
- [x] UX/design-language spec: `prd/10-ux-look-feel.md`.
- [x] `PLAN.md`, `STATE.md`, `megaspeed.example.com`.
- [x] **Deploy live**: nginx `megaspeed.example.com` in sites-enabled (nginx -t OK,
      reloaded); DNS `megaspeed.example.com → 203.0.113.10` added to
      `/etc/bind/example.com.zone` (dnsserver SIGHUP reloaded). Verified: DNS resolves,
      HTTPS route answers (502 until the dev server is up).
- [x] **Bitbucket DC test lab** resurrected (`bb-lab-a`), see below.

## In progress

- [ ] Walking skeleton (subagent `opencode/deepseek-v4.1-flash-free`): Bitbucket
      import + Timeline web UI + harness.
  - [ ] `ingest/` — Bitbucket DC client (PRs, commits, activities), ticket-key
        filter, fixture mode → DuckDB (pseudonymised).
  - [ ] `server/` — FastAPI + `/ws` + `/api/eval` + `/api/screenshot` +
        `/api/events`. **Must bind 127.0.0.1:8766** (nginx upstream).
  - [ ] `app/` — no-build canvas LTR timeline.

## Test lab (Bitbucket Data Center 9.4.18)

- Container `bb-lab-a` (via `sudo nerdctl`), `atlassian/bitbucket:9.4.18-jdk21`.
  - HTTP: `http://127.0.0.1:7990` (direct, not behind nginx).
  - Sysadmin: `admin` / `admin-lab-pw`.
  - Project `FIX` → repo `golden` (id 5).
  - Rich PR corpus for force-push / rescope / stacked / declined / squash edge
    cases: PRs #10, #9, #8, #6, plus declined + stacked branches.
    Activities stream confirmed working (OPENED / RESCOPED / COMMENTED /
    APPROVED).
  - Lab scripts: `~/dev/hans/lab/` (`lab-up.sh`, `lab-down.sh`, `config.env`,
    `SEED.md`); license at `lab/license.txt`.
  - **Note**: the `golden` corpus uses `feature/*`, `hotfix/*`, `experiment/*`
    branches — **no Jira ticket keys** in branches/commits/descriptions. Use
    bb-lab-a to test *real Bitbucket DC ingest mechanics*; test the ticket-key
    **filter** against megaspeed's synthetic fixtures and/or a real key format.

## Open items (blockers for real production data)

- [ ] Production Bitbucket DC base URL + project/repo(s) + auth (lab is dev-only).
- [ ] Jira ticket-key exact format (e.g. `FIX-\d+`) + which projects to scope.
- [ ] Dev-server port: **8766** (decided; reconcile with subagent output).

## Subagent model

- Primary: `opencode/deepseek-v4.1-flash-free`
- Fallback: `opencode-go/deepseek-v4.1-flash`

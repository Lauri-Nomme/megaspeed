# STATE — megaspeed progress & open items

## Done

- [x] Repo root moved to `megaspeed/` (was `megaspeed/prd/`); full history retained.
- [x] PRD complete: `prd/01..09` (context, pseudonymisation, data model, exports,
      analysis, architecture, milestones, risks, web UI).
- [x] UX/design-language spec: `prd/10-ux-look-feel.md`.
- [x] `PLAN.md`, `STATE.md`, `megaspeed.example.com` drafted.

## In progress

- [ ] First deliverable: Bitbucket import + Timeline web UI + harness.
  - [ ] `ingest/` — Bitbucket DC client (PRs, commits, activities), ticket-key
        filter, fixture mode, → DuckDB (pseudonymised schema).
  - [ ] `server/` — FastAPI + `/ws` + `/api/eval` + `/api/screenshot` + `/api/events`.
  - [ ] `app/` — SvelteKit timeline (canvas LTR, swimlanes, gantt bars, zoom).

## Open items (blockers for real data)

- [ ] **Bitbucket Data Center** base URL + project/repo(s) + auth (token/user).
- [ ] **Jira ticket-key** exact format (e.g. `DYNA-\d+`?) and which projects.
- [ ] Dev-server host:port to put in `megaspeed.example.com` upstream.
- [ ] Which nginx box hosts `megaspeed.example.com` (same `203.0.113.10` as immich?).

## Subagent model

- Primary: `opencode/deepseek-v4.1-flash-free`
- Fallback: `opencode-go/deepseek-v4.1-flash`

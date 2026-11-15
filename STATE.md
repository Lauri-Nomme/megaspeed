# STATE — megaspeed progress & open items

## Done

- [x] Repo root moved to `megaspeed/`; history retained.
- [x] PRD (`prd/01..09`) + UX spec (`prd/10-ux-look-feel.md`).
- [x] Deploy live (nginx + DNS). Walking skeleton + Playwright harness. Both QA reports.
- [x] **Corpus** — `bb-lab-b` licensed + `DEL` corpus (131 PRs, 100 tickets, 5 users, ~3 months).
- [x] **Ingest-fix** — 63 tests (was 13), `--verify` green: real DC `UPDATED`→`reviewer_changed`,
      incremental recompute, `MEGASPEED_TICKET_PROJECTS` prefix filter (no more `UTF-8`/`SHA-256`
      false positives), bot classification (`actor_kind`), tighter `--verify`, `PRREV`/`THREAD` objects.
- [x] **Fix + build UI** — all 14 UI bugs fixed (object lanes, provenance above fold, real lifecycle
      phases, filter=fade-not-delete, viewport survival, tooltip invalidation, axis, a11y, screenshot
      clone, zoom clamp, direction colours, empty state, persistence). Added backend
      `analysis/ballcourt.py` → `/api/blocking`, `/api/queues`, `/api/delivery`. Added views: **Blocking
      map, Queue view, Delivery list, Overview, Sources, Policy-lab stub** + shell (sidebar, functional
      scope/timeframe, scope banner, detail panel, pivot menu, Copy as Markdown, histogram brush→chip).
      Verified gantt phases == server state machine exactly. Tested on a second instance (8767).

## In progress (fallback `opencode-go/deepseek-v4.1-flash`)

- [ ] **Wire + verify ingest vs `DEL`** — run the fixed ingest against `bb-lab-b` DEL into a temp DB,
      confirm 131 kept / 0 dropped, `reviewer_changed`>0, bots, `--verify`, idempotency.

## Next (dispatch on completion)

1. **Server-fix pass** (`server/` + `app/`): clean 400s on `/api/eval`+`/api/screenshot` malformed
   input, restrict `screenshot.out` path, CORS, date-only `to=` inclusive end-of-day, unknown filters
   error (not silent), graceful DuckDB-lock 503 without leaking path/PID, and pagination/aggregation
   so the UI stops loading every event.
2. **Final integration**: restart 8766 on the new code, ingest `DEL` into the live store
   (`MEGASPEED_TICKET_PROJECTS=DEL`), point the UI at real data, smoke-test all views via the harness.

## Follow-ups (later)

- Light theme; shift-click/drag-box selection + pivot on graph nodes/queue rows; commits+tickets on
  timeline (G13); semantic zoom/epic nesting (G2); cost-labelled "load the full range" (G14);
  business-hours calendar (re-baselines waits); sweep-line queue WIP for 10k scale.
- `push_class` via git patch-id; question/phase LLM classification; Story/Epic/Sprint (Jira+Tempo).

## Test lab (Bitbucket DC 9.4.18)

- `bb-lab-a` :7990 — FIX/golden (no keys → negative filter cases).
- `bb-lab-b` :7991 — LICENSED, `DEL` corpus (positive cases). Users `admin`/`admin-lab-pw`,
  alice/bob/carol/dave/erin (pw `lab-pw-<name>`). License is a rolling ~3h dev window.
- Scripts: `~/dev/hans/lab/`, `megaspeed/corpus/`.

## Open items (real production data)

- [ ] Production Bitbucket DC URL + auth. [ ] Jira ticket-key format.

## Subagent model

- Primary: `opencode/deepseek-v4.1-flash-free` (exhausted) → Fallback: `opencode-go/deepseek-v4.1-flash`

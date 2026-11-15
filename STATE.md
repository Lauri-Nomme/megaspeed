# STATE — megaspeed · LIVE

The product is **live and working end-to-end**: `https://megaspeed.example.com/` (nginx → `127.0.0.1:8766`),
serving real Bitbucket DC `DEL` data — 131 PRs / 1316 events / 467 commits, `source=bitbucket-dc`.

## Working

- **BB ingest** — live DC 9.4, ticket-key filtered (`MEGASPEED_TICKET_PROJECTS`), pseudonymised store.
  Verified 131/131 kept, `reviewer_changed`=266, bots classified, `--verify` → `contract OK`, idempotent,
  no raw content (byte-level audit). 63/63 tests.
- **Views** — Timeline (1316 markers, 131 PR bars, 7 actor lanes), Delivery list, Blocking map (weighted
  graph, lens + drill-down), Queue view (WIP + slow/overloaded diagnosis), Overview, Sources, Policy-lab stub.
- **Shell** — sidebar, scope/timeframe, scope banner, detail panel, pivot menu + Copy as Markdown, histogram brush.
- **Harness** — Playwright headless browser + `/api/eval` + `/api/screenshot` (screenshot sandboxed).

## Built this session (coherent, committed)

PRD + UX spec → deploy (nginx+DNS) → bb-lab-a/b resurrection → walking skeleton → `DEL` corpus
(131 PRs, 100 tickets, 5 users, ~3 months) → 3 QA reports → 3 fix passes (ingest, UI+views, server) →
live integration → polish. All committed; `STATE.md` updated throughout.

## Next / follow-ups (prioritised)

1. **Business-hours calendar** — re-baselines every wait figure (currently wall-clock).
2. **`push_class` via git patch-id** — needs a local clone; unblocks rebase-waste analysis.
3. **question/phase local-LLM classification** — the original special requirement (prd/02).
4. Light theme · commits+tickets on timeline (G13) · semantic zoom (G2) · window-virtualised loading ·
   queue WIP sweep-line for 10k scale.
5. **Story/Epic/Sprint** objects (needs Jira Cloud + Tempo ingest) · SvelteKit shell.

## Known corpus limits (not ingest bugs)

- No `removedReviewers` / merge-commit block / thread resolution / sentinel slugs in `DEL`.
- 6 human actors (incl. `bb:admin`); `push_class`=`other` live (see follow-up #2).

## Test lab (Bitbucket DC 9.4.18)

- `bb-lab-a` :7990 — FIX/golden (no keys). `bb-lab-b` :7991 — LICENSED, `DEL`.
- Users: `admin`/`admin-lab-pw`; alice/bob/carol/dave/erin (`lab-pw-<name>`).
- `MEGASPEED_BB_BASE_URL=http://127.0.0.1:7991` etc. (see docs/DEV.md).

## Open items (real production data)

- [ ] Production Bitbucket DC URL + auth. [ ] Jira ticket-key format.

## Subagent model

- Primary `opencode/deepseek-v4.1-flash-free` (exhausted) → fallback `opencode-go/deepseek-v4.1-flash`.

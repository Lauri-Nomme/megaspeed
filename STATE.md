# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`). GitHub: `https://github.com/Lauri-Nomme/megaspeed` (private).
Install details (nginx site, public IP, DNS zone) are **scrubbed from the repo and all history**.

## Working

- **BB ingest** (airgap split: `scrape` → tgz → `import`) — 131 PRs / 1316 events / `reviewer_changed`=266 /
  bots classified / `--verify` OK / idempotent / zero raw content.
- **Views**: Timeline, Delivery list, Blocking map, Queue, Overview, Sources, Policy stub.
  Shell: sidebar, scope/timeframe + scope banner, detail panel, pivot menu + Copy as Markdown, brush.
- **Tenants**: one DuckDB store per dataset (`data/<name>.duckdb`); `/api/tenants`; `?tenant=` on all
  read endpoints; top-bar switcher. Live tenants: `del` / `live-probe` / `megaspeed`.
- **Tests**: **84** (63 core + 21 tenant tests). Harness (eval + screenshot) + Playwright wired.

## In progress

- [ ] **Scrape optimization** (prd/14): ticket-key pre-filter on the PR list + `updatedDate` watermark +
      settled-PR finality + NEWEST-first. Subagent implementing + adding a request-count test.

## Next

- [ ] Push the scrape-opt work when it lands; verify vs `bb-lab-b` `DEL` (correctness unchanged, requests cut).
- [ ] (If going public) genericize the local dev paths `/vokk/home/lauri/...` and the hard-coded
      `hans/lab/home/bb-lab-b` path in `corpus/backdate.py` — currently left as-is (see scrub caveats).

## Follow-ups (later slices)

business-hours calendar · `push_class` via git patch-id · question/phase local-LLM classification
(prd/02) · light theme · commits+tickets on timeline · semantic zoom · Story/Epic/Sprint (Jira+Tempo).

## Lab (Bitbucket DC 9.4.18)

`bb-lab-a` :7990 (FIX/golden, no keys) · `bb-lab-b` :7991 (DEL, licensed).

## Subagent model

Primary `opencode/deepseek-v4.1-flash-free` (daily quota, resets 00:00 UTC) → fallback
`opencode-go/deepseek-v4.1-flash`.

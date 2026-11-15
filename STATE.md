# STATE — megaspeed · LIVE

**Live**: `https://megaspeed.example.com` (nginx → `127.0.0.1:8766`).
**GitHub**: `https://github.com/Lauri-Nomme/megaspeed` (private).
Install config (nginx site + IP + cert layout) is **removed from the repo** — lives on the host only.

## Working

- **BB ingest** (airgap-ready split: `scrape` → tgz → `import`) — 131 PRs / 1316 events /
  `reviewer_changed`=266 / bots classified / `--verify` OK / idempotent / zero raw content.
- **Views**: Timeline, Delivery list, Blocking map, Queue, Overview, Sources, Policy stub.
  Shell: sidebar, scope/timeframe + scope banner, detail panel, pivot menu + Copy as Markdown, brush.
- **Tenants**: one DuckDB store per dataset (`data/<name>.duckdb`); `/api/tenants`; `?tenant=` on all
  read endpoints; top-bar switcher (switchable live, per-dataset scope memory). Default tenant = `del`.
- **Tests** 63/63. Harness (eval + screenshot) wired; Playwright headless browser.

## Airgap (prd/11, prd/13)

- **Scrape** (airgapped): source → classify → pseudonymise → **tgz** (`manifest.json` + `events.jsonl` +
  `objects.jsonl` + `sha256sums` + watermark). Watermark + identity map stay scrape-side; identity map
  never crosses.
- **Import**: tgz → DuckDB (idempotent by natural key).
- **Build**: `scrape/build.sh` → PyInstaller `--onefile` `megaspeed-scrape` (works locally).
- **CI**: `.github/workflows/build-scraper.yml` — builds on push/PR to `main`, path-filtered to the
  scraper component.

## Next

- [ ] Restart 8766 to activate tenants (del / live-probe / megaspeed) and the split.
- [ ] Verify airgap e2e: scrape `bb-lab-b` `DEL` → tgz → import → store.
- [ ] Push follow-up commits as they land.

## Follow-ups (later slices)

business-hours calendar · `push_class` via git patch-id · question/phase local-LLM classification
(prd/02) · light theme · commits+tickets on timeline · semantic zoom · Story/Epic/Sprint (Jira+Tempo).

## Lab (Bitbucket DC 9.4.18)

`bb-lab-a` :7990 (FIX/golden, no keys) · `bb-lab-b` :7991 (DEL, licensed).

## Subagent model

Primary `opencode/deepseek-v4.1-flash-free` (daily quota, resets 00:00 UTC) → fallback
`opencode-go/deepseek-v4.1-flash`.

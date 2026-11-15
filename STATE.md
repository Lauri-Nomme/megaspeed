# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), 3 tenants. GitHub: `Lauri-Nomme/megaspeed` (private, install-details scrubbed).
Tests: **215** (`tests/`) + **46** (`scrape/tests`) green.

## Scrape (airgapped) — full value in one pass

- **Ingest**: scrape → tgz → import; ticket **pre-filter** + `updatedDate` watermark + settled-PR finality +
  early termination (strict `<`), progress reporting. **Ticket-set scoping** (`MEGASPEED_TICKET_KEYS`/`_FILE`,
  exact keys, set-over-prefix) → DEL `DEL-100,DEL-101` = 3 PRs / 8 requests.
- **`push_class`** (shallow git cache, patch-id + range-diff). **LLM classification** (`comment_type`,
  `thread_outcome`, `question`+`phase`) via `qwen3-30b-a3b` on precision (~1.9 s/comment).
- **PR diff size** — `lines_added`/`lines_removed` per PR (shallow git cache).

## Analysis → suggested rules (the "what to change first" output)

`analysis/rules.py` → `GET /api/insights` → **Rules** view. 9 metrics → ranked suggestions (impact ×
confidence). DEL top-3: cycle-time (4.3 d median), rework-loops (p90 3 rounds), review-tail.
`rebase-waste` / `comment-tax` / `pr-size` are implemented but silent on the CURRENT DEL store (stale —
scraped before push_class + LLM existed; see below).

## LLM (precision box)

Ollama `qwen3-30b-a3b:2507` (30B MoE / ~3B active, CPU) — `http://10.43.36.10:11434`; logproxy `:11435`.

## In progress (fallback `opencode-go/deepseek-v4.1-flash`)

- [ ] **Re-scrape DEL `--full`** (push_class + LLM + diff size) → `data/del2.duckdb`, so the
      already-implemented `rebase-waste` / `comment-tax` / `pr_size` fire on real data.

## Next / follow-ups

- [ ] Business-hours calendar (re-baselines waits). [ ] Story/Epic/Sprint (Jira+Tempo).

## Open (production)

- [ ] Production Bitbucket DC URL + auth (configurable via `MEGASPEED_BB_*`). [ ] Jira keys
      (`MEGASPEED_TICKET_KEYS`/`_FILE`, e.g. `FOOBAR-1234`).

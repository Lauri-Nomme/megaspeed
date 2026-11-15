# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), 3 tenants. GitHub: `Lauri-Nomme/megaspeed` (private, install-details scrubbed).
Tests: **158** (`tests/`) + **46** (`scrape/tests`) green.

## Scrape side (airgapped) — full value in one pass

- **BB ingest** — scrape → tgz → import; ticket pre-filter + `updatedDate` watermark + settled-PR finality +
  early termination (strict `<`), progress reporting. 131/131 DEL, `--verify` OK.
- **`push_class`** — shallow/partial git cache, `git patch-id` + `range-diff` → content/pure_rebase/rebase_conflict.
- **LLM classification** — `ingest/classify.py`: mock + Ollama backends, closed vocab (`comment_type`,
  `thread_outcome`, `question` + `phase`), hybrid deterministic+LLM thread outcome, tgz emit, `--verify`.
  Real model `qwen3-30b-a3b:2507` bench: ~1.9 s/comment, 0 fallbacks; full DEL ≈ 471 calls ≈ 15 min CPU.

## LLM (precision box)

Ollama `qwen3-30b-a3b:2507` (30B MoE / ~3B active, Q4_K_M, CPU) in k3s `home-iot/ollama-moe`;
`http://10.43.36.10:11434` (svc) / `10.42.5.219:11434` (pod); `logproxy` sidecar `:11435`.
`:3000`/`:4000` are Mastodon. Weak GPU → small models only.

## CI

`.github/workflows/build-scraper.yml` — builds on main/PR, path-filtered to the scraper component;
**main push → tagged release with the `megaspeed-scrape` binary attached** (`scraper-<sha>`).

## Next

- [ ] **Analysis → suggested-rules layer** (prd/15): reviewer responsiveness (self-vs-other),
      time-to-first-review, PR-size vs latency, sentinel queue, comment-tax by type — the surface
      that turns the mined data into "what to change".
- [ ] Business-hours calendar. [ ] Story/Epic/Sprint (Jira+Tempo).

## Open (production)

- [ ] Production Bitbucket DC URL + auth. [ ] Jira key format.

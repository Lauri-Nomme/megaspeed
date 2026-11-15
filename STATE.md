# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), 3 tenants. GitHub: `Lauri-Nomme/megaspeed` (private, install-details scrubbed).
Tests: **102** (`tests/`) + **46** (`scrape/tests`) green.

## Working (scrape side, airgapped)

- **BB ingest** — scrape → tgz → import; ticket pre-filter + `updatedDate` watermark + settled-PR finality +
  early termination (strict `<`), progress reporting. 131/131 DEL, `--verify` OK.
- **`push_class`** — deterministic via shallow/partial git cache (`git patch-id` + `range-diff`);
  DEL → 199 content / 3 pure_rebase / 0 conflict; falls back to `other`, never crashes.
- **LLM classification** (in progress) — comment_type / thread_outcome / question+phase via local LLM.

## LLM (precision box)

Ollama `qwen3-30b-a3b:2507` (30B MoE / ~3B active, Q4_K_M, CPU) in k3s `home-iot/ollama-moe`;
`http://10.43.36.10:11434` (svc) / `10.42.5.219:11434` (pod); `logproxy` sidecar on `:11435`.
`:3000`/`:4000` are Mastodon (not the LLM). Weak GPU → small models only if adding more.

## In progress (fallback `opencode-go/deepseek-v4.1-flash`)

- [ ] **LLM classifier** (`ingest/classify.py`): mock + Ollama backends, closed vocab
      (comment_type / thread_outcome / question+phase), tgz emit, `--verify`, mock-first tests, benchmark.

## Next

- [ ] Analysis → suggested-rules layer (prd/15): reviewer responsiveness, time-to-first-review,
      PR-size vs latency, sentinel queue.
- [ ] Business-hours calendar. [ ] Story/Epic/Sprint (Jira+Tempo).

## Open (production)

- [ ] Production Bitbucket DC URL + auth. [ ] Jira key format.

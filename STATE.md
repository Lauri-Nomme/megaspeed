# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), tenants. GitHub: `Lauri-Nomme/megaspeed` (private, install-details scrubbed).
Tests: **321** (`tests/`) + **46** (`scrape/tests`) green. CI builds a tagged release of the scraper.

## Scrape (airgapped) — full value in one pass

Ticket-set scoping (`MEGASPEED_TICKET_KEYS`/`_FILE`) · pre-filter + watermark + settled-PR finality +
early termination · `push_class` (shallow git cache, patch-id/range-diff) · diff size (`lines_added/removed`)
· LLM classification (`comment_type`, `thread_outcome`, `question`+`phase`) via `qwen3-30b-a3b` on precision.
Import is a **pre-write gate** (validates the whole bundle, rolls back on failure) and batched (~90× faster).

## Analysis → suggested rules

`analysis/` → `GET /api/insights` → **Rules** view. 9 metrics → ranked suggestions. **Business-hours
calendar** (prd/19): per-person overrides, vacations, EE holidays incl half-days, `business_hours()`
applied everywhere, vacation-blocking insight. `hours_mode` = business when a calendar applies.

## Reviews + fixes (this cycle)

- **Product review** (`docs/review-product.md`) · **adversarial review** (`docs/review-adversarial.md`) ·
  **QA round-2 UI/UX** (`docs/QA-round2.md`).
- **Fixed (High)**: SEC-1 import gate · SEC-2 eval/ws bridge auth · FR-1 watermark vs never-seen PR ·
  NFR-1 import speed · NFR-2 timeline loads newest · UI: DEL-1 label, scope banner, queue `author`,
  drill-down · responder-vs-blocker attribution · dependents · DES-3 honest thread_outcome · censoring.
- **Corpus** now exercises everything: rebases (pure + conflict), thread resolution, sentinel (`frank`),
  reviewer removal, merge commits, varied PR sizes.

## Next

- [ ] **Fresh scrape of the NEW corpus** (bb-lab-b DEL re-seeded with all features) → `data/del3.duckdb`,
      so `rebase-waste`/`comment-tax`/`pr_size`/`sentinel-queue` fire on real data. (The `del2` re-scrape
      was killed — it was scraping the pre-reseed corpus.)
- [ ] Roadmap: Jira+Tempo (Story/Epic/Sprint) · stacked-PR cascade · flow-efficiency/survival ·
      simulation · deferred UI items (B6/B9–B21, G2–G12 in `docs/QA-round2.md`).

## LLM (precision box)

Ollama `qwen3-30b-a3b:2507` (30B MoE / ~3B active, CPU) — `http://10.43.36.10:11434`; logproxy `:11435`.

## Open (production)

- [ ] Production Bitbucket DC URL + auth (`MEGASPEED_BB_*`). [ ] Jira keys (`MEGASPEED_TICKET_KEYS`).
- [ ] Lab license expired — read-only works, pushes need the clock-shift shim (see `corpus/README.md`).

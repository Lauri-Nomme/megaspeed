# 11 — Airgapped ingest (scrape → transfer → import)

The ingest must support **airgapped transfer**: the scraper runs next to the source
(which may be isolated/offline), and hands the megaspeed main component a **tgz of
already-pseudonymised data** plus a watermark. All classification — deterministic and
local-LLM — happens on the scraper side, so nothing raw ever crosses the boundary.

## 1. Two sides

| Side | Runs | Does | Keeps |
| --- | --- | --- | --- |
| **Scraper** | airgapped, next to source | scrape source · classify (deterministic + local cheap LLM) · pseudonymise · emit tgz | watermark cursor, raw→pseudonym identity map, config, (later) local git clones |
| **Main** | this env | import tgz → store · analyse · serve UI | DuckDB store, analysis, web UI |

This is the same raw→pseudonymised boundary as `02-pseudonymization.md`, made physical.
The main side has no source credentials and never sees raw content.

## 2. Transfer contract (the tgz)

```
export/
  manifest.json      # schema_version, source(s), watermark_ts, classifier_version,
                     #   projects/repos, counts, created_at
  events.jsonl       # pseudonymised events — one JSON object per line
  objects.jsonl      # PR / COMMIT / PRREV / THREAD / TICKET / PERSON objects
  sha256sums         # integrity
```

- `events.jsonl` / `objects.jsonl` rows are **the same shape as the current store rows**
  (`event_key`, `ts`, `activity`, `actor`, `objects`, `attributes`, …), so import is a
  straight load — no re-shaping.
- The tgz contains **no identity map**. The raw→pseudonym map is the linkability
  artifact and stays on the scraper side (see §5). It is pseudonymised *by construction*.

## 3. Watermark & incremental semantics

- **Scraper input state (files):** the watermark (per-source max event timestamp), config
  (sources, `MEGASPEED_TICKET_PROJECTS`, bot slugs), the identity map, classifier version.
- **Scraper output:** events with `ts > watermark` plus a small **overlap window** (to
  survive backdated/late events), and the **new watermark = max ts emitted**.
- **Import is idempotent by natural key** (`event_key`) — already true of the store — so
  overlap and re-transfer are safe. The watermark is a *checkpoint/efficiency* concern,
  not a correctness one.

## 4. Classification happens scrape-side

- Deterministic (`git patch-id`, regex, schema) **and** the cheap local LLM
  (question/phase/severity) run on the scraper. The local LLM's model files must be
  shipped into the airgapped env (no internet).
- Consequence: **classifier upgrades are scrape-side re-runs** (re-classify → re-transfer),
  not main-side. Re-analysis of already-transferred data with a new classifier version
  requires a fresh scrape.

## 5. Identity & pseudonym stability

- The identity map persists scrape-side, so the **same actor keeps the same pseudonym**
  across incremental transfers (`LaNo`, `ToBr`, …). The main side relies on this.
- Losing the identity map breaks pseudonym continuity → it is the one piece of
  scrape-side state that is irreplaceable and must be backed up.
- Identity map never enters the tgz. (If it ever must cross, that's a separate,
  explicitly-reviewed path — not the default.)

## 6. Verification on import

Before loading, the main side checks: `schema_version` compatible · `classifier_version`
known · watermark **monotonic** (new ≥ previous) · checksums · `--verify` contract
(closed vocabulary, no raw text, no canaries). A failing tgz is rejected, not merged.

## 7. Implications for the current code

- Split `ingest/` into **scrape** (source → classify → pseudonymise → write tgz) and
  **import** (tgz → DuckDB). The existing reducer output *is* the tgz content; the
  existing `store.write_events` *is* the import path — the split is mostly plumbing.
- `--live` (direct BB) becomes a **dev shortcut** = scrape + import in one process (no
  real airgap); production is always scrape → transfer → import.
- `push_class` via `git patch-id` (currently deferred) belongs on the **scrape** side,
  next to the local git clone.

## 8. Open decisions

1. **JSONL vs Parquet** for the tgz payload — JSONL recommended for portability/debuggability;
   Parquet as a size optimisation later.
2. **Overlap-window size** (how far back to re-scrape before the watermark) — default e.g.
   1–7 days, configurable.
3. **Per-source watermark** vs a single timestamp — BB activities `createdDate`, Jira
   changelog, Slack `ts` have different cursor semantics; likely one watermark per source.
4. Whether to keep a **raw** side-channel at all — default: no; raw never leaves the scraper.

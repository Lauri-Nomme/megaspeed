# 08 — Risks & open questions

## Risks

### Pseudonymisation / opsec (the big one)

- Raw content — descriptions, comments, diffs, commit messages — can hold secrets,
  IP, and re-identifiable phrasing. The contract (prd/02) keeps it from ever
  entering the store or any LLM view; `--verify` enforces it. This is the core
  requirement, not an afterthought.

### Goodhart's law

Once people know what's measured, they optimise it. Mitigate by: measure system
outcomes (cycle time, flow efficiency), not individual throughput; revisit
metrics as behaviour shifts.

### User interface

- **Raw-content leakage in the UI** — the web UI is read-only over the
  pseudonymised store; tooltips and labels render only keys, pseudonyms, and
  closed attributes. The canary test extends to every rendered view (raw strings
  must never appear).
- **Timeline performance at scale** — ~10k+ events need level-of-detail
  aggregation (coarse zoom = counts/densities, fine zoom = events) and a
  canvas/WebGL renderer (not DOM/SVG), or the UI stalls. Zoom/pan are
  load-bearing, not polish.

### Data quality

- **Timezones** — normalise everything to one TZ or the wait times are garbage.
- **Bot accounts** — filter before any human-latency metric.
- **Mixed identities** — a person with two accounts must resolve to one pseudonym.
- **Reopened/retargeted PRs** — must not double-count or corrupt the DAG.
- **Incremental re-analysis** — incremental ingest must be idempotent (natural
  keys) or re-runs double-count; snapshots must be `as_of`-reproducible so
  metrics stay correct as items close over time.
- **Worklog noise** — measured directly (createdAt − dateStarted gap), never trusted.

### Classification risk

- **Paraphrase leakage** — the cheap LLM "summarising" instead of "labelling".
  Mitigated by closed vocabulary + JSON-schema constrained decoding + schema
  rejection at parse time. This is the single most important failure to design
  against (see `02-pseudonymization.md`).
- **False positives in question detection** — acceptable; we tune the label set
  rather than risk leakage.
- **Taxonomy drift** — new question/comment types appear; `other` bucket queues
  cases for human taxonomy extension.

### Sampling

- **One epic is a small sample.** Mine the team's full history, then zoom in on
  the epic. Don't draw conclusions from N=1.

## Open questions

1. **Local LLM model choice** — qwen2.5 vs llama3.2 3B vs gemma3 vs phi-class;
   needs a small benchmark on *our* comment/question data (run locally, on
   sample text that never leaves the raw zone).
2. **Raw retention policy** — immediate delete vs short-TTL encrypted vault for
   re-tuning the classifier. Needs a decision either way.
3. **Slack DMs** — in scope (metadata-only, with consent) or out? They hold
   blocking context but are the most sensitive source.
4. **Project-key canonicalisation** — ticket keys are allowed, but a project key
   can reveal a product. Accept for v1, or canonicalise `PROJ→P1`? Trade-off:
   readability vs a little more privacy.
5. **Working-hours calendar source** — how do we source holiday/part-time data
   without a new data-collection burden?
6. **Refinement calendar source** — manual list, or infer from recurring
   meetings?
7. **Merge-condition fidelity** — can we reconstruct the exact sentinel
   merge-check config from Bitbucket, or do we need it documented by the team?

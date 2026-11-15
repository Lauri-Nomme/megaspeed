# 08 — Risks & open questions

## Risks

### Privacy / legal (the big one)

- **GDPR + works council.** Person-level blocking graphs are sensitive. Mitigate
  by: agreeing scope with HR and the works council *before* scraping; presenting
  at role/queue level; pseudonyms everywhere; framing as *system* improvement,
  never people-ranking.
- **Works councils are the norm** in an Austrian company context. This project
  must be a transparent, opt-in, process-improvement initiative — the works
  council is an ally here, not an obstacle, but only if approached first.

### Goodhart's law

Once people know what's measured, they optimise it. Mitigate by: role-level
presentation, not individual dashboards; measure system outcomes (cycle time,
flow efficiency), not individual throughput; revisit metrics as behaviour shifts.

### User interface

- **Raw-content leakage in the UI** — the web UI is read-only over the
  pseudonymised store; tooltips and labels render only keys, pseudonyms, and
  closed attributes. The canary test extends to every rendered view (raw strings
  must never appear).
- **Timeline performance at scale** — ~10k+ events need level-of-detail
  aggregation (coarse zoom = counts/densities, fine zoom = events) or the UI
  stalls. Zoom/pan are a load-bearing feature, not polish.

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
   re-tuning the classifier. Needs legal sign-off either way.
3. **Slack DMs** — in scope (metadata-only, with consent) or out? They hold
   blocking context but are the most sensitive source.
4. **Works-council approval process** — what does it actually require here, and
   what's the timeline? This gates slices 1–5.
5. **Project-key canonicalisation** — ticket keys are allowed, but a project key
   can reveal a product. Accept for v1, or canonicalise `PROJ→P1`? Trade-off:
   readability vs a little more privacy.
6. **Working-hours calendar source** — how do we source holiday/part-time data
   without a new data-collection burden?
7. **Refinement calendar source** — manual list, or infer from recurring
   meetings?
8. **Merge-condition fidelity** — can we reconstruct the exact sentinel
   merge-check config from Bitbucket, or do we need it documented by the team?

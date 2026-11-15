# 02 — Pseudonymisation (the special requirement)

This is the load-bearing wall of the whole design. Everything else is standard
process mining; this is the part that makes it legal, defensible, and safe to
run analysis with an open-source LLM.

## The rule, stated plainly

The scraped data and the primary datastore (or any view of it) may contain:

| Allowed | Forbidden |
| --- | --- |
| Ticket keys (`PROJ-1234`, PR IDs, commit hashes) | Ticket **content** / descriptions |
| Abbreviated actor IDs (`LaNo`) | Commit **messages** |
| Enumerated **classifications** of content | **Diffs** / source code |
| Timestamps, status transitions, point values, sprints | Comment / review thread **bodies** |
| Counts, severities, phases (from a fixed taxonomy) | Slack **message text** |
| | Any free-text paraphrase or "summary" of the above |

Two subtleties worth writing down before they bite us:

1. **"Ticket keys are OK" is a deliberate, accepted residual risk.** A project
   key can reveal a product name. We accept ticket keys *as-is* for the first
   release and revisit canonicalising project keys in the open questions.
2. **A summary is NOT a classification.** A cheap LLM asked to "summarise this
   comment" will happily leak the comment's content in softer words. That is
   still a leak. **Classification is a projection onto a pre-agreed closed
   vocabulary** — a label from a finite set, never a sentence. "Generalised
   classification" means *which bucket*, not *a shorter version*.

## Why this matters (threat model)

The raw content (descriptions, comments, commit messages, diffs, Slack text)
contains three kinds of things we must not put in front of an open-source LLM:

- **Personal data** — names, the kind of thing GDPR and the works council care
  about, and the reason people-level output must be pseudonymised anyway.
- **Secrets / IP** — design rationale, incident details, credentials pasted into
  a comment, unreleased product decisions in a diff.
- **Linkability** — even without names, distinctive phrasing re-identifies a
  person or a project.

The open-source LLM used for analysis is, by definition, *not something we
control end-to-end*: it might be a hosted endpoint, a model that gets fine-tuned,
or a vector store that gets backed up somewhere. So the invariant is:

> **No human-authored text ever crosses the local boundary.**
> Raw content is reduced to labels *inside* the building, and only the labels,
> keys, and pseudonyms enter the primary datastore that the LLM can ever see.

The classifier that *reads* the raw text runs locally, on a cheap model. The
analysis LLM never reads the raw text because it never has it.

## The pipeline boundary

```
  ┌───────────────────────────  RAW ZONE (local, ephemeral) ───────────────────────────┐
  │                                                                                    │
  │  Jira / Bitbucket / Slack scrape  ──►  transient staging                            │
  │                                             │                                      │
  │                                             ▼                                      │
  │   local classifier:  deterministic (git patch-id, regex, schema)                   │
  │                      + cheap local LLM (closed-vocabulary, JSON schema)            │
  │                                             │                                      │
  │                                             ▼  (only keys + pseudonyms + labels)   │
  └─────────────────────────────────────────────┬──────────────────────────────────────┘
                                                │  ║ firewall of policy (not just network)
  ┌──────────────────────────  PSEUDONYMISED ZONE (analysis) ──────────────────────────┐
  │                                             ▼                                      │
  │   primary datastore (OCEL event log, DuckDB/Parquet)  ──►  analysis (SQL + LLM)   │
  └────────────────────────────────────────────────────────────────────────────────────┘
```

- **Raw zone** is a single machine / container, no cloud, no external model API.
  Raw files are deleted after classification (see data lifecycle below).
- **Pseudonymised zone** is the only thing that can be shared, backed up,
  pushed, or handed to an open-source LLM.

## Closed vocabulary (the taxonomy)

The classifier may emit **only** values from this finite set, plus keys and
pseudonyms. There is no "notes" field in the primary schema. If the classifier
cannot map something to a label, it emits `other` and queues the case for a
human to *extend the taxonomy* — it never falls back to free text.

| Dimension | Closed values |
| --- | --- |
| Activity type | `created`, `status_changed`, `assigned`, `pointed`, `sprint_added`, `sprint_removed`, `comment_added`, `worklogged`, `pr_opened`, `pr_pushed`, `pr_approved`, `pr_unapproved`, `pr_needs_work`, `pr_merged`, `pr_declined`, `reviewer_changed`, `build_status`, `question_raised`, `question_answered`, `rebased` |
| Question phase | `breakdown`, `refinement`, `sprint`, `implementation`, `review` |
| Comment severity | `mandatory`, `suggestion` |
| Push class | `content`, `pure_rebase`, `rebase_conflict` |
| Linkage target type | `ticket`, `pr`, `none` |
| Bot vs human | `bot`, `human` |

Attributes are typed and enumerated. An event is
`(ts, activity, actor_pseudonym, [object_ids], {closed attributes})` — nothing
stringly-typed beyond keys and pseudonyms.

## Deterministic classification first, LLM second

The **local cheap LLM is the component that does *raw → generalised
classification***: it reads raw content locally and emits only labels from the
closed vocabulary. But it is used for natural-language signals **only** — the
default is **no LLM** wherever a deterministic method exists:

| Signal | Method |
| --- | --- |
| Push → content/rebase/conflict | **deterministic**: `git patch-id --stable` on `target...old` vs `target...new`; `git range-diff` to confirm conflict hunks |
| Ticket-key / PR-URL detection in Slack | **deterministic**: regex |
| Bot vs human | **deterministic**: known account set |
| Status / assignee / sprint / points | **deterministic**: Jira changelog schema |
| Comment severity | **LLM** (BLOCKER/task vs suggestion) — or Bitbucket task-type where available |
| Question raised / answered + phase | **LLM** (description/comments/Slack/PR comments) |
| Slack message → ticket/PR linkage | **deterministic regex first**, **LLM** only for the unlinked remainder |

The local LLM: a small model (qwen2.5 / llama3.2 3B, gemma3, phi-class) served
via **Ollama / llama.cpp**, temperature 0, **constrained decoding against the
JSON schema** of the taxonomy, few-shot with the label set. We accept a few
false positives on question detection; we do **not** accept content leakage, so
the schema enforces the closed vocabulary at the parsing layer, not as a
prompting convention.

## Pseudonym scheme

Deterministic, one-way-in-practice, stable across all three sources:

- **Actor ID**: first two letters of given name + first two of surname,
  e.g. **Lauri Nomme → `LaNo`**. Collisions get a deterministic disambiguator
  (`LaNo`, `LaNo2`, …) assigned by the identity-resolution step, so the same
  person is *always* the same pseudonym regardless of source.
- The **identity-resolution table** (Jira accountId ↔ Bitbucket slug ↔ git
  email ↔ Slack ID ↔ pseudonym) lives **only in the raw zone**. Downstream sees
  pseudonyms, never the mapping.
- We do not pseudonymise ticket keys or PR IDs (explicitly allowed), but we
  *do* pseudonymise anything human-shaped (display names, email prefixes) even
  when they'd otherwise leak through a git author string.

## Data lifecycle

1. **Scrape** → raw staging (local, temporary).
2. **Classify** → emit pseudonymised events; record `classifier_version` and a
   content hash alongside each event (so outputs are reproducible/tunable
   *without* the raw text).
3. **Discard** → raw staging deleted on a schedule (default: immediately after
   classification). Optional encrypted raw vault with a short TTL is a
   future, separately-approved thing — **never** part of the analysis view.
4. **Audit** → a log of "what did the classifier emit for which object" so a
   misclassification can be traced and the taxonomy extended, again without
   needing the raw text back.

## Proving the guarantee

- **Schema enforcement**: the primary store has **no free-text columns** for
  content. Attempting to insert one is a build/test failure, not a review point.
- **Canary test**: a set of planted, unique raw strings run through the pipeline
  must never appear anywhere in the pseudonymised store or any LLM view.
- **Boundary test**: the analysis layer has no network path to the raw zone and
  no credentials for the source systems — it can only read the pseudonymised
  store.

## What this buys us

Because the analysis LLM only ever sees keys + pseudonyms + closed labels, we
can use a **fully open-source model** for the interesting reasoning (who blocks
whom, what to change) without shipping anyone's prose anywhere. The part that
needs the prose is tiny, local, cheap, and constrained to produce labels.

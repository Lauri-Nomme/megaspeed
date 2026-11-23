# 76 — Replies as first-class events (v0.21 design)

Status: **implemented (v0.20.6).** Ingest emits one `comment_added` per nested
reply (`reply=true`, `reply_index`, `parent_comment_id`) on the opener's object
set, with no labels. Every existing reader excludes them (ballcourt/jira/query,
review rounds, approvals-lost), so the event log is complete while the metrics
stay identical. `python -m ingest backfill-replies --hans-scrape DIR --identity
PATH [--apply]` fills an existing store from the capture, idempotently.

## Measured gap

```
threads                         12,938
threads with >= 1 reply          5,311  (41%)
nested replies in the source     5,832
comment_added events for replies     0
question_answered events           156  (the only trace a reply leaves)
```

The pipeline emits `comment_added` for an **opener** only; a reply inside a
comment is not an activity. Consequences:

- a human reply inside a bot/CI thread is invisible after ingest (the v0.20.1
  label survives, the author and timestamp do not);
- the comment tax measures openers, so 41% of threads' dialogue is unmeasured;
- `question_answered` is one model call on the first reply, not a timeline;
- no aggregate can ask "did a human answer the bot and when".

The bundle and the trace cannot backfill this: the raw reply text is discarded
at ingest by design. Only the capture still has it.

## Design

Emit one `comment_added` event per nested reply:

| attribute | value |
|---|---|
| object | the same `THREAD:` object as the opener |
| `reply` | `true` on a reply; the opener carries `false` (or nothing) |
| `reply_index` | 1-based position in the thread |
| `parent_comment_id` | the comment the reply hangs under |
| actor / ts | the reply author's pseudonym and `createdDate` |

Label policy (cost control):

- replies are **not** model-labelled by default (+5,832 calls otherwise);
- the first human reply of a bot/CI thread keeps exactly the v0.20.1 treatment:
  one call, label stored on the THREAD with `label_source=reply`;
- a later opt-in (`--classify-replies`) can label every human reply; not in the
  default path.

Aggregates must keep their meaning: anything that means "review comments written
by people" filters `reply != true` (comment tax, delivery, queues, per-person
comment counts, activity charts). The analysis layer gains an explicit
`openers` view; the event table stays complete.

## Migration

A `replies` refresh pass re-reads the **capture** (no git walk, no model) for the
already-imported PRs and emits the missing reply events. Idempotent on
`(thread_id, reply_index)`. Stores without the capture keep
`reply_events: 0` and every metric behaves exactly as before.

## Acceptance

- A thread with two replies has three `comment_added` events, exactly one with
  `reply=false`.
- Comment-tax, delivery and queue counts are **unchanged** by the new events.
- A bot thread's human follow-up is visible (actor + ts) and the THREAD carries
  `label_source=reply`.
- `question_answered` is derivable from the reply timeline, not a one-off model
  read.
- The contract check passes: only closed attributes, no raw text.

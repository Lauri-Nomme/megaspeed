# 78 — The deterministic no-fit filter

Status: **implemented (v0.22.0).** The gold-set ≥90%/≤2% measurement is still a laptop/auditor step; the predicate's true/false examples are pinned in `tests/test_citext.py`.

## What “no-fit” means

“No-fit” is not “no nit”. It is text that fits **none of the six review
classes** and that no reviewer would call a remark about the code:

- rebase / merge / conflict notes (“rebased”, “merged master”, “resolved
  conflicts”, a commit link);
- review requests (“please review”, “PTAL”, “@X can you look”, “LGTM”, “+1”);
- author status notes (“closing this”, “will add the fix”, “done”, “updated the
  PR”, “I’ll try another approach”, “nvm, handled”);
- bare links, images/attachments, “fyi”, “heads up”, “see above”, “same here”;
- bot-style commands (`@bot review`) that escaped the alias skip.

The v1 audit put ~25% of human `out_of_scope` there; the v3 audit found 39% of
its changed-row sample was no-fit. They are the rows that shuffle between
`suggestion`, `nit` and `out_of_scope` when a prompt changes, so they add noise
to every transition table and corrupt the comment-tax denominator. They also
caused the o2 optimization edit to fail: its real gains were swamped by +8
no-fit rows labelled `optimization`.

## Decision

A **deterministic pre-model filter**, not a prompt rule and not a new
`comment_type`:

- match only when the **whole text** is a notice/status/request shape: anchored
  at the start, no code fence, no inline code, bounded length (~400 chars);
- set `comment_kind=admin` and **no** `comment_type` (the existing `admin` kind,
  already used for rebased/bare-link rows); emit no model call;
- the comment_added event and its thread stay in the log (the text is dialogue,
  the label is not a review remark);
- if a rule matches but the text contains a concrete defect phrase, do not
  filter (err on the side of sending it to the model).

The filter is conservative on purpose: a false positive removes a real review
remark from the comment tax, while a false negative costs one model call.

## Replies are exempt — this is the important boundary

The filter applies to **opener** text only. A nested reply is never filtered,
because a reply is where the author states what became of the thread:

- “fixed in the next push”, “won't fix, by design”, “nvm, handled” are not
  no-fit administrative notes; they are the **intent/outcome evidence** the
  outcome ladder and `thread_intent` need (prd/75);
- the opener's review label and the thread's outcome are separate outputs: a
  thread can have a review remark for a label and a status note for its outcome;
- the reply event must stay in the log (it does), and the raw text must reach the
  intent classifier before it is discarded, so the filter must not remove it
  from the model input.

Consequences for the implementation:

- the pre-model check is `role == "opener"` **and** the no-fit predicate;
- the first-human-reply label on a bot/CI thread may still skip a status note
  for the *review label* (a status note is not a review remark), but that skip
  must not be reused for the **intent** pass: the last human reply is classified
  for intent and bypasses this filter;
- `recategorize` reads only opener records (`role == "opener"`), so the store
  migration cannot touch a reply;
- if a reply is classified for intent and the text is also a review remark, the
  intent call may return `comment_type` as well; the filter still does not apply
  to it.

## Rules (to be measured, not guessed)

`ingest.citext` gains `is_notice(text)` (or `is_admin` is extended; the PRD
prefers a named predicate):

| family | shape |
|---|---|
| rebase/merge | `rebased`, `rebase`, `[rebased]`, `merged <branch>`, `resolved conflicts`, + an optional link |
| review request | `please review`, `PTAL`, `can/could someone review`, `@handle review`, `LGTM`, `+1`, `ship it` |
| author status | `closing`, `done`, `fixed in <sha>`, `added/updated <thing>`, `will (add|fix|try|push|do)`, `I’ll …`, `going to …` |
| link/attachment | a bare URL, an image attachment, “see above”, “same here” |
| admin | “fyi”, “heads up”, “note:”, “thanks” (whole text only) |

Existing `is_admin` rules (rebase/same/depends/related/bare URL/image) stay and
are subsumed.

## Where it applies

- **Scrape/reducer**: before the model, in the same place `citext.is_admin` is
  checked now; set `comment_kind=admin`, skip `_comment_labels` — **openers
  only**; `_first_human_reply` and any reply-intent path are outside it.
- **Existing store**: `recategorize` already reads the opener text from the
  trace; the filter sets `admin` and removes `comment_type`/`classifier_version`
  for the matched human threads. A dry run reports the counts first.
- **Bot/CI**: unchanged (`ci_notice`/`ci_finding`/`bot` already precede this).

## Acceptance (measure before shipping)

On the frozen gold set (`v3` iteration `gold.jsonl`: 97 no-fit rows, 156 real
rows):

- **≥90% of gold-nofit caught**;
- **≤2% of real review text caught** (≤3 rows of 156);
- no `blocker`, `question` or `optimization` gold row is filtered;
- the store dry run reports the same `admin` count the eval predicts (±5%);
- a second `recategorize --apply` changes nothing;
- a status-note **reply** is still sent to the intent classifier and is not
  marked `admin` by this filter.

If the filter misses the target, narrow the rules; do not widen them to hit the
percentage.

## Non-goals

- No new `comment_type` (`other`) — the kind axis is enough.
- No prompt change; the model must not be asked to detect notices.
- No deletion of events or text; only labels.

## Files

`ingest/citext.py` (predicate + tests), `ingest/pipeline.py` (pre-model), 
`ingest/recategorize.py` (existing store), `tests/test_citext.py`,
`tests/test_recategorize.py`.

## Why it matters now

It cleans the label denominators, removes a known source of prompt-edit noise,
and unblocks the **o2 optimization re-screen**: with the gold-nofit rows out of
the input, o2’s +3 optimization rows and HIGH F1 .75→.91 can be judged on real
cost remarks rather than on rebase notes.

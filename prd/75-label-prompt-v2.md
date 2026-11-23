# 75 — Label prompt/schema v2 (draft, after the 2026-11-23 audit)

Status: **draft.** The audit of the first model relabel (`relabel-audit-report`)
found the model clearly better on `comment_type` (83% of decisive human samples)
and mostly better on questions, but exposed systematic noise that a prompt and a
deterministic pre-filter should remove before the next model pass.

## Findings to fix

1. **Bots/notices are not review text.** One coverage bot posted 2,277 openers
   (2,247 coverage reports, 29 Qodana); the model gave the same template four
   different labels, and `CaGi41` had been merged with a human by the suffix
   rekey (now split: `CaGi41` is `bot`, `CaGi` is the human). Bots are 21.6% of
   openers.
2. **No `other` class.** Rebased notices, bare links, "added reviewer", fyi,
   LGTM, emoji, AI-bot commands. They fit none of the six review classes.
3. **Intent is asked of the opener.** `thread_intent` is “what became of the
   thread”; the opener cannot know it. The model books status notes and praise
   as `acted`.
4. **The `out_of_scope` few-shot pairs `rejected`.** 512/719 out_of_scope
   openers inherit that intent and drive most new `rejected` outcomes (now
   guarded by keeping diff-derived outcomes).
5. **Mild defects become `nit`/`question`.** "this is always false now", "this
   call doesn't work" — a statement that the code is wrong/misbehaves is a
   blocker even when phrased briefly.
6. **Perf over-read.** `optimization` only when cost (time, memory, allocation)
   is the point; coverage percentages are not performance.
7. **Description question adds.** 6 rows, 0/2 sampled right; require a real `?`
   outside URLs/code, or drop PR adds.
8. **AI-bot commands are labelled question.** 88/139 "review/verify/explain"
   commands to an AI reviewer; deterministic `@`-command pre-filter.

## Design

**Deterministic pre-filter (no model):**
- skip/annotate by bot actor (`actor_kind=bot` from the store, or a configured
  raw slug / pseudonym list such as `CaGi41`);
- skip/annotate coverage/Qodana templates and `@`-bot commands;
- these threads get **no review label** (or a future `notice` class), never a
  guessed `nit/optimization/blocker`.

**Closed vocabulary:** add `other` (a valid remark that is not one of the five
review classes) **or** keep six classes and give non-review notes
`suggestion`+`unclear` with an explicit prompt rule. Prefer `other` if the
taxonomy review accepts the wider vocabulary; it needs analysis/UI support.

**Prompt/schema:**
- remove `thread_intent` from the opener call; derive outcome deterministically
  from the persisted `anchor_changed` (v0.16+ bundles), or classify the last
  reply when intent is genuinely needed;
- fix the `out_of_scope` example to `intent=unclear`;
- add mild-defect blocker examples (“this is always false now”, “doesn't work
  without an unwrap”);
- optimization only for cost; deferrals are out_of_scope;
- descriptions: question only for a real `?` outside URLs/code.

**Re-run:** a prompt/schema/model change is a fresh model pass (the label cache
is keyed by model/think/version, not prompt text; bump the classifier version).
Keep the same audit strata and seeds, add a bots-excluded stratum per transition,
and a small human dual-rated blocker subset (the audit’s blocker recall ~65% is
the weakest class).

## Acceptance

- Bot/notice openers have **no** review label (or `other`), and their share of
  the six classes drops to ~0.
- Mild-defect blockers: a sample of 30 statements of broken code gets `blocker`
  at ≥80%, with ≤10% false escalation.
- `optimization` on human text is cost-only (audit sample 12:5 → ≥80%).
- Description question adds only with a real question mark outside URLs/code.
- The next audit reports the same per-transition tables **bots excluded**, and
  the outcome distribution is unchanged from the store’s diff-derived values.
- No raw text in the public repo; labels only.

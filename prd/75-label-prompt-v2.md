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

## Proposed taxonomy v2 (awaiting sign-off)

`comment_type` answers "what does this reviewer want from the author". CI output,
bot commands and administrative notes have no such want, so they must not share
the axis — mixing them is what produced 1,093 bot "nit" and 922 bot
"optimization" in the first pass and corrupted the comment-tax denominators.

**Keep `comment_type` for human review remarks only.** Add a second, entirely
deterministic attribute on the THREAD object:

| `comment_kind` | who / what | `comment_type` | actionable |
|---|---|---|---|
| `review` | a human review remark | one of the six | per type |
| `ci_notice` | CI output with no action (coverage report, Qodana clean) | none | no |
| `ci_finding` | CI output that creates work (Qodana issues, coverage below threshold) | none | yes → author |
| `bot_request` | a human addressing a bot (`@bot review`, `@bot wdyt …`) | none | no (the reply may) |
| `bot_answer` | the bot's reply inside such a thread | none | no |
| `admin` | rebased / added reviewer / LGTM / fyi / emoji / bare link | none | no |

- `comment_kind` is set by rules (actor kind, configured raw bot slugs, template
  regexes, `@`-command prefix), never by the model; a bot's two templates must
  not get two labels.
- `ci_finding` vs `ci_notice` **cannot come from `build_status`.** This corpus
  has **0 `build_status` events**: the scrape reads
  `BUILD_STATUS_API/commits/{id}` (`bitbucket.py:205`), which is real build
  servers only; the coverage and Qodana bots post ordinary `comment_added`
  events and never write a build status. There is no structured CI result to
  join against, so the distinction must be parsed from the bot's **own
  template**, deterministically:
  - identify the family by bot actor + template regex (coverage percentages;
    `Qodana: …`);
  - **coverage = `ci_notice`, always.** The trace shows 2,247 coverage comments
    from one account (21% of all openers) in 9 template variants — percentages,
    covered ratios, one- and two-metric subsets, a header-only form (18), and an
    emoji form (`:100: % coverage :+1:` = 💯%, 487). The measured distribution
    is **positive, not alarming**: instruction p50 = 90% (n=1,713, 387 at 100%),
    line p50 = 90% (n=1,687, 455 at 100%), branch p50 = 77% (n=1,442, 229 at
    100%); only 73 of 1,742 reports have every metric at 0. None carries a
    pass/fail token, none states a threshold, and all say "for the first build",
    so there is no earlier report on the same PR to diff against. There is no
    deterministic basis for a finding, so the rule must not invent one: store
    the parsed metrics (`coverage.instruction|line|branch`, percent) and
    `metrics_zero` for the 73 (the diff report had nothing to score, or the
    report failed — not a review verdict). This alone removes the 1,093 `nit` +
    922 `optimization` + 156 `out_of_scope` + 76 `blocker` labels the first pass
    put on a bot.
  - **Qodana has exactly two templates** (30 comments, all from the bot):
    `#### ⚠️ Qodana detected N new issue(s) … Please do not merge without
    solving/suppressing` (20) → `ci_finding`, `issues=N`, `actionable=true`,
    `blocking=true` (the bot states the merge rule itself); `#### ✅ Qodana: no
    new issues found.` (10: 9 with report links, 1 without) → `ci_notice`,
    `issues=0`.
  - **Both conditions are required: bot actor *and* template.** One human opener
    (`MeMi`) begins with "qodana", and the text-only regex puts it in the CI
    bucket; a human discussing a Qodana result is a normal review remark and must
    stay one.
  - a bot comment that matches **no** known template is `ci_notice` with
    `template=unknown`, counted so a missing rule is visible — never a guess.
- **Human boilerplate is `admin`, deterministically**: `rebased` / `rebase` /
  `[rebased]` / `Rebased & resolved conflicts` / `Rebased & solved conflicts`,
  `same as` / `same` / `same here`, `depends on`, `related to`, a bare URL, an
  image attachment. These are ~120 openers that currently receive a review
  type from the model; they are status notes, not remarks.
  If a real `build_status` exists in some future source it is corroboration, not
  the source; it must not be the only path, because this corpus proves that path
  is empty.
- The bot filter itself is a config gap, not a missing design: `pipeline.py:849`
  already skips `actor_kind=bot` before the model, but the scrape's
  `MEGASPEED_BOT_SLUGS` did not hold the coverage/Qodana account, so the raw
  user record classified as human and 2,277 openers went to the model. Their
  `comment_added` events still say `"actor_kind":"human"`; the PERSON row was
  corrected to `bot` only during the 2026-11-23 split. Add the raw slugs.
- `actionable=true` on `ci_finding` is the "ball goes to the PR author" flag, so
  the queue/blocking graph can carry it without pretending it is a `blocker`.
- `bot_request` must be excluded from question statistics: 88/139 `@…review`
  commands were booked as `question` (audit), which is neither a question to the
  author nor a review remark. Its size is small: 112 openers mention the AI
  reviewer in prose (the other 37 `@` hits in the trace are Java annotations such
  as `@Override`/`@Nullable`, which is why a slug list is required).
  **The bot answers as *its own* comment, and the first `replies` sample was
  too old.** 50 of the 112 openers were sampled in trace order — PRs #1556–#1863,
  long before the reviewer bot existed — so 0 answered there. The store has a bot
  pseudonym (`AgJu`) with 1,049 `comment_added` events, **1,025 of them on PRs
  ≥10000**, from 2026-06-29 onward, and it answers questions too. So a
  `bot_request` opener can be followed by a bot comment that is a *new thread*,
  not a nested reply: count it by **actor** and PR timeline, never by the
  mention's reply list. And the `@alias` in the text is not the account name —
  classify the author with the product's `classify_actor` (SERVICE type,
  configured slugs, service-shaped names) instead of the handle. Sample by PR
  range or shuffle; trace order is oldest-first.
- `admin` replaces the "no class fits" escape hatch. No `other` value is needed
  in `comment_type` once the kind axis exists; a human non-review note becomes
  `comment_kind=admin` with a null type.

Consequences: `comment_type=null` stops meaning "not labeled yet" and starts
meaning "not a review remark", so the store/insights must report the
`comment_kind` mix and compute comment-tax shares over `kind=review` only. The
current "mixed labels" disclosure becomes "N review / M ci / K admin".

## Diff context at the comment anchor

The model currently sees `text` and `kind` only (`OllamaClassifier.request_body`
puts context in the trace, not the prompt). It should also see the code the
comment points at:

- **What:** the `-U8` hunk at `commentAnchor.path:line`, from
  `git diff <base>..<head_at(comment_ts)>` restricted to that path — i.e. the
  code the reviewer was looking at, not the final head (which may already be the
  fix). Pass `path`, `line`, `lineType` (ADDED/REMOVED/CONTEXT) and the language.
- **Why:** the audit's worst classes are all code-dependent — a one-line "this
  is always false now" is a blocker, "rename this" is a nit, "this allocates per
  row" is optimization, "where is this defined?" is a question. Text alone cannot
  separate them.
- **Prompt shape:** append the hunk to the user message under an explicit
  "context, not the claim" header; never put it in the system prompt.
- **Cache:** the label cache key must include a hash of the hunk (and path/line
  type), otherwise a prompt that adds a hunk silently reuses text-only labels.
  Bump the classifier version for the first hunk-enabled pass.
- **Cost:** roughly doubles prompt tokens; measure with the `reclassify`
  subcommand before a full pass (0.33 texts/s is the current no-diff rate).
- **Failure modes:** unknown head across a RESCOPED gap (fall back to text-only,
  record `diff=absent`), minified/generated paths (skip), and the model inventing
  defects from code (keep the rule "the text is the claim, the hunk is context").

Outcome is a separate matter: the inputs (`anchor_changed`,
`resolver_is_commenter`) already exist at scrape time and the audit's regression
was the relabel builder discarding them (fixed in v0.19.2). The hunk is for
`comment_type`/`question`, not for re-deriving outcomes.

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

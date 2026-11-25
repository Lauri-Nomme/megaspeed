# Audit task — is the v2 relabel better than v1?

You have a **new model trace** (`hans-relabel-v2.tgz.trace.jsonl`), its patch
(`hans-relabel-v2.tgz`), and a stratified sample (`v2-audit.jsonl`) produced by
`audit_hypotheses.py sample`. Judge, from the text, whether **v2** is better than
**v1** on the threads where the label changed.

Local work only. The sample holds real review prose: **do not paste it into any
repository or report**; use thread ids and paraphrases.

## Context

- v1 (`ollama-v1`) was the first model pass; it was clearly better than the mock
  on `comment_type` (83% of decisive human samples) but noisy on
  non-review text (a coverage bot got `nit`/`optimization`) and guessed
  `thread_intent` from the opener.
- v2 (`ollama-v2`) changes the prompt only:
  - a short "this is always false now" is a `blocker`;
  - `optimization` only when cost is the point;
  - the `out_of_scope` example no longer pairs `rejected`;
  - `thread_intent` is `unclear` unless the text itself states the outcome.
- v2 **skipped** CI/admin/bot-directed rows deterministically; they are not in
  this trace and must not be judged here.
- The anchor diff is **not** an input (A/B: 3 label changes in 21, two
  reciprocal → no signal).

## Inputs

- `sample` row: `{thread_id, pr_id, old, new, labels, text}` — `old` is the v1
  label that is in the store, `new` is v2.
- The closed vocabulary is unchanged: blocker / suggestion / question / nit /
  out_of_scope / optimization; `question`, `answered`, `phase`.

## Questions

1. Per transition (top 15 by volume): is v2 right more often than v1? Use
   `v2 better` / `v1 better` / `both ok` / `both wrong` / `unclear`.
2. The classes v2 was meant to fix:
   - mild defects (`nit`/`question` → `blocker`): did v2 escalate real defects,
     and how often did it over-escalate?
   - `optimization`: is it now cost-only?
   - `out_of_scope`: still used for deferrals, not for "I disagree"?
3. `question`: v1 added 829 and removed 1,414 against the mock. Does v2 keep the
   good removals and the good adds?
4. `thread_intent`: is `acted` now rare and text-supported? Any remaining guess?
5. Distribution: compare v2's shares with v1's on the same stratum. Any class
   that collapses or explodes is a prompt bug, not a corpus fact.
6. Recommendation: import v2, keep v1, or re-run a third time on one class.

## Method

- Judge **per transition**, not from totals.
- Sample size is small (≈20/transition); report counts, not just percentages.
- If a text is ambiguous, use `unclear`; do not invent ground truth.
- The v1 label is **not** truth: it is the thing being compared.

## Output

```
# v2 audit — model v2 vs v1

## Verdict
- v2 better / mixed / worse
- import / re-run / keep v1
- confidence + limitations

## Per transition
| old -> new | n | v2 better | v1 better | both ok | both wrong | unclear |

## The classes v2 targeted
- blocker escalation: ...
- optimization: ...
- out_of_scope: ...
- question: ...
- intent: ...

## New failure patterns
...

## Recommendation
1. ...
```

## Constraints

- Read-only; do not modify the trace, the patch or the store.
- No raw text in the report; ids and paraphrases only.
- A transition that only swaps two adjacent classes (`nit`↔`out_of_scope`) is
  usually noise; say so instead of counting it as a win.

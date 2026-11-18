# 36 — Ticket lifecycle honesty & labels

The ticket decomposition's *arithmetic* is right, but its *labels, headlines and omissions* mislead.
Conform to `docs/concepts.md` §5.

## Findings solved (docs/QA-exploratory.md)

- **#3** "ready → refined" is really **created → ready** (`analysis/jira.py:82/103`, `ticket.js:27` calls
  it "backlog"). The real ready→refined gap is ~0.2 h. Drives rule #1 and every ticket headline.
- **#4** "review rounds" swallows idle time between two PRs — DEL-155 shows 123.6 h, ~60 h of it has no
  PR open. The headline says "62 % in review rounds".
- **#5** the ticket headline ignores the open PR and the Done state — DEL-167 is Done with PR #53 open
  192 h; #53 appears only as a table row.
- **#6** rule-1 "100.1 d per story" is the 2.4 k-hour total ÷ 24, not per story (≈ 28 h/story).
- **#13** reopens are invisible — 5 stories were `done→ready`; DEL-105 shows a DONE marker on 26 Aug, a
  "closed 31 Aug" header, and a "took 108.67 h" headline with no mention.
- **#14** "9/8 segments measured" — `app/main.js:987` hard-codes `/8`.
- **#16** "rounds" definition drift: rules count pushes+unapprovals ("16 PRs past 3 rounds") while the
  store's `review_rounds` says 1.
- **#16** "rounds" definition drift — rules count pushes+unapprovals ("16 PRs past 3 rounds") while the
  store's `review_rounds` has 1; *and* a rebase that loses approvals is double-counted (analysis = 2
  rounds for one rebase: the push **and** its push-induced `pr_unapproved`), while a target-shift
  approval-loss with no push is counted by the analysis but not the store.
- **#22** "logged a week late" includes `within_week` (2–7 d late).

## Fix (conforms to concepts.md §5)

1. **Rename the first segment** to `created → ready` everywhere (analysis, ticket.js, rule text); the
   segment-bound vocabulary already says `created`→`refined`.
2. **Segment honesty for multi-PR gaps** — when a segment spans an interval with no PR open (e.g. a
   second PR opened after the first merged), the segment reports its "no PR open" sub-span explicitly,
   and the one-line answer names it ("second PR started 11 days after the first merged") instead of
   folding it into "review rounds".
3. **Headline honesty** — the headline states the ticket's real end state and any open/censored PR:
   "Done in Jira, but PR #53 is still open (right-censored)". The cycle is measured over the merged PRs;
   an open PR is disclosed, not hidden.
4. **Reopens visible** — a story with a `done→ready` move shows "reopened" and reports both created→
   first-done and created→final-done, never silently one.
5. **Fix the counts** — "N of M segments measured" derives M from the actual segment count (9, not a
   hard-coded 8); "per story" means a per-story statistic (median/mean), never an aggregate total
   mislabelled as per-story; **"rounds" uses the concepts.md definition — `# pr_pushed (after first
   review) + # pr_unapproved with cause ≠ "push" (after first review)` — in one place, so a
   rebase-with-approval-loss is one round (not two), a target-shift/manual approval-loss is one round,
   and the store's `review_rounds` and the analysis agree** (use the `cause` attribute the ingest
   already stores); "logged a week late" excludes `within_week`.

## Tests

- Segment label is `created → ready`; a multi-PR ticket with an idle gap reports the gap honestly and
  the headline names it; a reopened story reports both done boundaries; "N of M" = 9/9 when all are
  measured; the per-story figure is a statistic; rounds and worklog-lateness use the corrected
  definitions.

# 46 — Final-QA reconciliation (segments, diagnosis consistency, rework/worklog)

The final pass (`docs/QA-final.md`, 6 questions) confirms the *counts* reconcile but surfaced a third
generation of issues — the biggest is a real correctness bug in the ticket decomposition. Conform to
`docs/concepts.md`.

## 1. Out-of-order segment boundaries (correctness, do first)

`analysis/jira.py` `_cycle` accepts boundary timestamps in any order, so DEL-185/117/151 show a
"61 % in sprint → in progress" headline whose segments **sum to 174–200 % of the cycle** — overlapping
or wrapping boundaries. **Fix**: clamp/order the boundaries (a boundary earlier than its predecessor is
reported honestly, not silently drawn as a negative/overlapping segment), so a segment's hours never
exceed the cycle and the segment shares sum to ≤ 100 %; the one-line answer names any out-of-order
boundary as a data quirk rather than a finding.

## 2. One canonical sentinel-wait number (diagnosis consistency)

Frank's wait reads as 1.2× / 2.2× / 3.1× / 5.6× across views. **Fix**: pick the gate-aware response time
(last-peer-answer → sentinel-action, the prd/43 clock) as the single canonical "sentinel wait", state its
definition next to every occurrence, and reconcile the other surfaces to it (or label each as a
*different* metric). Fix the "never answered 24 (63 d)" count to exclude bot-closed/declined PRs that
were never the sentinel's turn.

## 3. Rules page internal contradiction

The #1 card (refinement, assumed impact) sits beside a banner saying the lab models +82 %; "cap at 3"
binds zero PRs while the lab's lever is cap-at-2. **Fix**: the ranked list and the cross-check banner
must agree — demote/adjust a contradicted rule so the #1 it presents is the one its own evidence and the
lab's note actually support, and make the round-cap rule use the cap the lab's best scenario uses (2).

## 4. "Committed" = committed at sprint start

A sprint card's "committed" must count stories added **at or before the start** (the `added_after_start`
signal prd/42 added already exists); stories added mid-sprint are "adopted", not "committed".

## 5. Worklog honesty reframe

The worklog card's headline blames late logging (2.5 % over a week late); the real signal is that logged
hours don't track points (r ≈ −0.05). **Fix**: surface the logged-vs-points (and logged-vs-active) gap as
the headline, keep lateness as a secondary line.

## 6. Papercuts + missing (smaller, grouped)

- Layout: delivery inspector must not cover the table/truncate the ticket column; un-truncate the queue
  response numbers.
- Wording: "87th of 87 fastest" → "slowest of 87"; "idle" chips → a clearer label.
- Missing features worth a follow-up (not this PRD): repo scope filter, a trend-over-time view, a
  story-level rework list, an active-sprint at-risk view, a pseudonym legend.

## Definition of done

Segments sum ≤ 100 % and out-of-order boundaries are flagged; the sentinel wait is one number with one
definition; the Rules #1 agrees with its own banner and lab note; "committed" = at-start; the worklog
headline is the points/active gap; the papercuts are fixed. Each with a regression test. Conform to
`docs/concepts.md`. Keep 963 + 99 + 231 green.

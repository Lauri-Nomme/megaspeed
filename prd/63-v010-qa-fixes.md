# 63 — v0.10.0 QA fixes (both passes)

Two parallel QA passes on `v0.10.0` (`docs/QA-v010.md` question-based, `docs/QA-explore.md`
free-exploring) agree on one thing: **the raw numbers are accurate, but the product is not shippable
as-is** — there are high-severity bugs (hangs, collapsed UI, wrong links) and several numbers that
mislead across scopes. This PRD fixes the concrete findings. Conform to `docs/concepts.md`; follow
`prd/10`.

## A. High-severity bugs (fix first)

1. **Trend hangs with a repo scope.** The client sends `repo=` and `/api/trend` rejects it (400), so the
   page stays "Loading the trend…" forever — and the scope persists, so a reload stays stuck.
   **Fix:** `/api/trend` accepts the `repo` scope (like every other analysis endpoint, prd/49) and
   computes per that cohort; the client's trend fetch passes through the active scope.
2. **Unknown / malformed ticket hangs.** `#ticket/NOPE-1` (deep link) and `DEL-9999` (search) leave
   "Loading the ticket…" forever — the server returns a clear 404/400 but the UI ignores it.
   **Fix:** the ticket page renders a stated "no such ticket" (with the key) on a 404/400, never an
   infinite loader; a malformed key is rejected client-side.
3. **Worklog bars collapse to 2 px.** `.worklog-bars` is defined twice in `app/style.css` (~1618 and
   ~2166) and the later rule wins. **Fix:** one rule; the ticket-page "logged vs active" bars and the
   Rules worklog panel render at their intended height (they currently read "0 min" stubs while the
   headline says 3 h 30 min — same root cause).
4. **Copy-as-Markdown drops the tenant.** Links copied from `del3` open the default tenant (a different
   dataset). **Fix:** every generated link carries `?tenant=<current>` (or the current store), so a
   pasted link lands on the same tenant.
5. **Row context menu misbehaves.** "Open in timeline" switches page but ignores the item; "Clear scope"
   always jumps to Timeline. **Fix:** both act on the row that was right-clicked (open a timeline scoped
   to the ticket/PR; clear scope in place, not a page jump).
6. **"chase →" link contrast.** In the dark theme it's the browser default blue on dark grey (1.51:1).
   **Fix:** use the link token (WCAG ≥ 4.5:1) like every other link.

## B. Misleading numbers (correctness)

7. **Scopes contradict on Overview.** Scoped to a person/repo, the header says "8 open · 1 blocked" while
   the tiles say 0, "MEASURED WAIT" stays at the team total (304 d), and "Done in Jira, PR open: none" is
   false (3 team-wide). **Fix:** every Overview tile/honesty line recomputes on the active scope, and the
   "Done in Jira, PR open" list is scope-aware (or states "3 team-wide").
8. **Trend uses the empty in-progress bucket as "latest."** Merged PRs shows "0 latest, −2 since first
   bucket" (a −2 y-tick) and the "in progress" label clips; sprint on-time becomes a 1-vs-1. **Fix:** the
   latest *measured* bucket is the headline, the in-progress bucket is excluded and labelled (never a
   delta), and the axis has no negative tick.
9. **The "Done in Jira, PR open" set isn't reachable from Delivery.** The list clips its `stale` /
   `unobserved` pills, and the Overview link lands on the Delivery list which has no such section.
   **Fix:** the pills don't clip; the Overview link lands on a filtered Delivery view (or the section
   exists there).
10. **A declined PR shows `504 h (median 24.8 h · 20.4×)`.** 504 h is the window cap and the PR is a
    bot-declined zombie that was never delivered work. **Fix:** a declined/abandoned PR's age is not
    benchmarked against the merged-PR cohort; show "declined" (or exclude from the contrast).

## C. Diagnosis consistency (the recurring theme)

11. **No single constraint verdict.** The Overview (person-level) names Frank; the role-level view names
    the review queue; "wait on Frank" is 6 h / 75 d / 165 d depending on the view. **Fix:** one canonical
    constraint answer with one clock — this is prd/58; until it lands, label each surface's metric
    ("response time" vs "queue-wait hours") and stop calling both "the constraint". (Coordinates with
    prd/58.)
12. **Overview "5 orphaned approvals — nudge FrMo" is wrong for 2 of 5** (#53 waits on CaOs, #66 on its
    author), and the "chase →" link only opens the ticket page. **Fix:** the orphan list uses the real
    holder(s) per PR, and the link opens the chase action (or the PR drawer), not just the ticket.
13. **Sprint "only 2 committed / 28 adopted" miscounts** stories added 30 min after the start, and
    "44 % by points" is the done share, not the adopted share. **Fix:** the committed/adopted split uses
    the sprint's start day (not the exact timestamp), and the points share is stated for the right
    population.
14. **Rules #08/#09/#10 recommend things their own lab note says don't work.** **Fix:** a rule whose lab
    note contradicts it is demoted/badged (prd/46 §3 already does the rank; this closes the remaining
    cards), and clipped cells (e.g. "who owes" on the chase card) don't truncate.

## D. Smaller papercuts

- Delivery "waiting on" and Overview "who owes" must agree for the same PR (DEL-183).
- The "median ×" toggle on Delivery says nothing when the cohort is < 10 — show the reason
  ("cohort too small"), not silence (prd/57 §small-N).
- The PR-size sliders change the counts but never produce a PR-size rule — either the rule fires with
  the chosen caps or the panel says why not.
- Columns truncate when there is room — fix the width rules.
- Phone: Timeline and Ticket overflow horizontally (`scrollWidth > 412`); the build tag overlaps content.

## Definition of done

Every A/B/C item has a regression test (or a source guard where only wiring); each D item is fixed. The
Trend page loads under a repo scope; an unknown ticket shows a stated error; the worklog bars render; a
Copied link keeps its tenant. Keep 1038 + 99 + 291 green; verify at 1600 px and 412×915. A re-run of both
QA passes should find none of the A/B items.

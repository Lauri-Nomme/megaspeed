# 63 — v0.10.0 QA fixes — COMPLETE checklist (every finding from both passes)

Sources: `docs/QA-explore.md` (free-exploring, **F1–F30**) and `docs/QA-v010.md` (six questions,
**V-A1…A12**, **V-B**, **V-C**). This is the full inventory — every finding, grouped by severity, each
with its fix. Conform to `docs/concepts.md`; follow `prd/10`. IDs are traceable to the reports.

## A. High-severity bugs (fix first)

- **F1** Trend hangs under a repo scope — `/api/trend` 400s on `repo=`, UI spins forever and persists.
  Fix: `/api/trend` accepts `repo` (cohort-scoped); a failed fetch renders an error state, never a spinner.
- **F2** Unknown/malformed ticket hangs — `#ticket/NOPE-1`, `DEL-9999`, `foo bar` → "Loading…" forever.
  Fix: render the server's 404/400 ("no such ticket in tenant X" + hint + back); reject a malformed key client-side.
- **F3** Ticket-page "Worklog vs active" bars collapse to 2 px — `.worklog-bars` defined twice in
  `app/style.css` (~1618 and ~2166); the later wins. Fix: one rule; ticket bars + Rules worklog panel render.
- **F4** Row context menu — "Open in timeline" ignores the item; "Clear scope" jumps to Timeline even from
  another view. Fix: both act on the right-clicked row (scope the timeline to it; clear in place).
- **F5** "chase →" link unstyled in dark theme (`#0000EE` on `#292B2E`, 1.51:1). Fix: link token, ≥4.5:1.
- **F6** Copy-as-Markdown drops the tenant from links (`del3` → default tenant). Fix: every link carries
  `?tenant=<current>`; the Delivery export title names the active tab ("Blocked: 1 of 8").

## B. Misleading numbers (correctness)

- **V-A1 / F10** "Who is the constraint" has no single answer (6 h / 75 d / 165 d; person vs role vs
  response). Fix: label each surface's metric (response-time vs queue-wait), and coordinate the single
  verdict with prd/58 (do not build prd/58 here).
- **V-A2 / F9 / F11 / V-A11** The Overview "5 orphaned approvals — nudge FrMo" is wrong for 2 of 5 (#53
  waits on CaOs, #66 on its author), the "chase →" only opens the ticket page, "waiting on" (Delivery) ≠
  "who owes" (Overview) for DEL-183, and a scoped Overview can say "no item is waiting" above a 2-day
  orphan. Fix: real holder(s) per PR, one answer across surfaces, scope-aware wording ("none in this scope").
- **V-A3** Sprint "only 2 committed / 28 adopted" is a strict-timestamp artefact (3 added 30 min after
  start); "44 % by points" is the done share. Fix: committed/adopted on the sprint's start **day**, and
  state the points share for the right population.
- **V-A4 / F3** WORKLOG panel bars read "0 min" stubs while the headline says 3 h 30 min (the duplicate-CSS
  bug). Fix: same as F3.
- **V-A5 / F8** Trend uses the empty in-progress bucket as "latest" (`0 latest, −2 since first`), a negative
  count tick, a clipped "in progress" label, and a censored latest cycle bucket. Fix: headline = last
  complete bucket with min N; exclude/label the in-progress bucket; clamp count axes at 0; show N.
- **V-A6 / F17** Ticket headline for a Done-with-open-PR story brags ("13th fastest of 87 · 0.8× · 9/9")
  while 62 % of its change is unmerged. Fix: name the unmerged share; don't call a censored cycle "measured".
- **V-A7 / F17** Segment labels hide what the time was: "review rounds" contains 60 h with no PR open;
  "verification → done 31 %" is the reopen loop; "active" 6.7 h vs 0 h. Fix: state the no-PR-open sub-span,
  label the reopen loop, reconcile "active".
- **V-A8 / F11** Queue "NEVER 8 (21 d 4 h)" for FrMo = every open PR because a peer comment opens the gate
  (5 of 8 have no peer approval). Fix: the gate opens on "peer answered", and the count distinguishes
  "waiting only on the sentinel".
- **V-A9** Sprint delivery never shows team velocity per window (S2: 15/52 pts by membership vs 23/84
  actually finished in S2's dates). Fix: show both, crediting carry-overs.
- **V-A10 / F28** Rules advice contradicts the lab on the same card (#08 refinement +82 %, #09 smaller PRs
  +289 %, #10 sentinel −3 %); #04 is a correlation (r=0.62). Fix: demote/badge contradicted rules (prd/46 §3
  rank already does part); label #04 as correlation.
- **V-A12** People-diagnosis flags 6 of 6 people (overloaded/slow) against a 2.5 h baseline. Fix: only flag
  a real outlier (e.g. > 2× median), not everyone.
- **F7** Person/repo scope contradicts itself on Overview (header "8 open · 1 blocked" vs tiles 0; MEASURED
  WAIT stays team-total; "Done in Jira, PR open: none" false team-wide; Delivery "4 stale · 8 unobserved"
  team-wide; "Scope person LaAd (person LaAd)" label duplicate). Fix: every tile/line recomputes on scope;
  scope-aware wording; dedupe the label.
- **F13** A declined PR shows `504 h · 20.4×` (window cap on a bot-declined PR vs the merged cohort). Fix: no
  benchmark multiple on declined/abandoned/censored spans.
- **F12** "median ×" toggle on Delivery does nothing when the cohort is < 10 — show "needs ≥ 10 items".
- **F22** "Snapshot from N calendar days ago" rounds 3 d 15 h up to "4". Fix: round the stated calendar days
  correctly (or say hours).
- **F10** Overview rule-of-thumb counts: "5 orphaned approvals" vs Rules "2 items waiting only on FrMo";
  "24 stories carried over" vs sprint strip "27". Fix: reconcile (same threshold/basis) or state the reason
  inline.

## C. Papercuts (UI / interaction)

- **F14** Silent fallbacks: `?tenant=nope` → default silently; `view=bogus`/`tf=zzz`/`scope=person:Zed` →
  silently Overview/1q/all; tenant switch resets a person/role scope unannounced; `/api/meta?tenant=` is
  rejected (can't ask `del3`). Fix: a one-line notice on a fallback; accept `tenant` on `/api/meta`.
- **F15** Scope options that do nothing (e.g. `queue ready`, or a scope on Rules/Policy/Sources/Trend/
  Blocking). Fix: hide/disable a scope on a view that ignores it, or state "not filterable".
- **F16** All 8 open items carry `unobserved` (noise when it's true for all); ages priced to a 4-day-old
  snapshot. Fix: only show `unobserved` when it's informative; state the snapshot age once.
- **F17** Ticket detail: boundaries not chronological (DEL-183: `REFINED 13:20` before `READY 13:00`) with no
  flag; raw attribute dump in TICKET EVENTS; `· other` unlabelled; del3 (no STORY) shows "0/8 segments"
  while listing a merged PR. Fix: order + flag, render attributes as labelled facts, label `summary_class`,
  explain the no-STORY case.
- **F19** Keyboard/focus: no "skip to content"; a row click opens the drawer but focus stays on the row;
  click-behind doesn't close the drawer. Fix: focus the drawer on open, Esc/click-behind restores, add a
  skip link.
- **F20** Blocking people vs role give different totals (304 d vs 340 d) with the same "mean"; the Overview
  uses the role number silently; Queue response-table rows aren't clickable though the hint says they are.
  Fix: state the basis; make the table rows match the hint.
- **F21 / F27** Policy lab: implementation jargon (`service_share`, `queue_recovery`, `P-K`); presets
  *replace* knob state while the helper says they combine; no loading state; scenario not in the URL;
  Sources window (`27 Jul→16 Nov`) differs from the top bar and Rules ("103.6-day"), no per-source breakdown.
  Fix: user-facing words; presets compose or say "replaces"; a run spinner; scenario in the URL; one window
  basis; per-source rows.
- **F23 / V-B** Column truncation at 1600 px (chase "who owes" clipped to 92 px with 700 px empty; "Done in
  Jira" WHO OWES and NOTE overflows the viewport; Queue "(median 3 h 2 mi…"; Policy "≈2 h 45 …"; Timeline
  sprint-at-risk pills; Delivery status chips). Fix: width rules/flex so the actionable part isn't clipped.
- **F24** Timeline is mostly prose at 1600×1000 (canvas below the fold); S3 strip says `8 Oct → 18 Nov`
  (snapshot) while the ticket page says plan end 27 Nov; clicking a selected sprint row doesn't deselect.
  Fix: raise the canvas, state plan-end vs snapshot, toggle deselect.
- **F25** Phone (412×915): Timeline `docSW=419`, Ticket `415` (overflow); legend chips off-screen; sprint
  rows clipped; Queue table wider than the viewport; the build tag overlaps content; inline ticket keys in
  Timeline prose are 46×13 px tap targets. Fix: no horizontal overflow, wrap/scroll the legend, reposition
  the build tag, ≥44 px targets.
- **F26** Queue: header "WIP sampled every 24 h" vs footer "every 23 h 44 min"; with `person:LaAd` it says
  "0 streams" but the response table still lists everyone; the explanatory paragraph is repeated twice.
  Fix: one interval label, filter the table, dedupe the paragraph.
- **F28** Rules: the "people" toggle is a no-op (team rules repeat); snake-case ids as titles; Markdown
  grammar ("7 PRs opened before the window and is not in the cohort"); PR-size sliders rescale after the
  first input (thumb jumps) and no `pr_size` rule ever appears though the footer says the caps drive it.
  Fix: real people filtering, human titles, grammar, stable slider ranges, and the rule fires or explains.
- **F29** Both themes: empty-slot separators ("· · business hours"); the "adopt a review SLA" paragraph is
  flush to the card border; visiting a `?theme=light` link persists it as the user's default (dark only via
  the toggle). Fix: separators, padding, don't persist a link's theme as the stored preference.
- **F30** Trend state isn't in the URL (metric + week/sprint); a `sprint=` deep link on Delivery keeps the
  pill pressed but doesn't filter. Fix: URL round-trip; honour the sprint scope.
- **V-B** (remaining): carry-over line is an unreadable 27-item run-on; mixed duration formats ("73.3 h" /
  "192 h" vs "21 d 0 h"; "24 business days"; "802 calendar days" under a business-hours banner); Policy-lab
  "REQUESTS ≈640" is a 6-replication sum; cryptic labels (`median ×`, `×3.1`, `· other`, `unobserved`,
  `stale`, `orphaned`); developer jargon on the ticket page ("prd/21 decomposition", "prd/02", "concepts.md
  §1"); `#ticket/del-167` (lowercase) 404s while the search normalises; the ticket page has no links to
  Timeline/Delivery/Blocking or to its PRs outside the table; timeframes mean different things per card
  (`1w` shows live-state criticals); 19 of 100 stories carry a quirk flag shown only in prose. Fix each.

## D. Missing features (from the passes; some already PRD'd)

- **V-C1** constraint verdict + "what first" that picks one lever and rejects the others → **prd/58**.
- **V-C2 / V-C4** side-by-side repo comparison; active-sprint landing forecast → prd/49 (repo) + prd/52/60.
- **V-C3** sprint velocity per window (stories/points finished in the sprint's dates, carry-overs credited)
  + plan-day grace — add to prd/37/52 (small).
- **V-C5** rework as a rate / first-pass yield + unmerged-share-of-change → **prd/62** (first-pass yield).
- **V-C6** ticket page links to PRs/timeline/blocking; "waiting for a reviewer to be added" as its own
  interval — small, add here.
- **V-C7** Trend significance/N shading, censored markers, per-repo/per-person trend, a text verdict →
  prd/50/61.
- **V-C8** aging/stale report incl. approved-then-declined PRs → **prd/59**.

## Definition of done

Every A/B/C item has a regression test (or a source guard for pure wiring); every D item is either fixed
here or already covered by its PRD. Re-running both QA passes finds none of the A/B items. Keep
1038 + 99 + 291 green; verify at 1600 px and 412×915.

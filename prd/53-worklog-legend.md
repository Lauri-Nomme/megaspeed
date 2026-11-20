# 53 — Worklog-vs-points honesty + pseudonym legend

## User journey
A lead asks "can we trust the worklogs?". The worklog card's headline blames *late logging* (2.5 % over a
week late), but the real signal is that **logged hours don't track points** (r = −0.05; a 13-pt story
logs 2–3 h) and total 0.49× of active time. Separately, a lead can't tell who the sentinel is without
reading the Queue footnote.

## Main design
Two small additions:
1. **Worklog-vs-points**: the worklog card (and a small chart) shows logged hours against story points
   (and logged-vs-active), with the flat-vs-points gap as the headline and lateness as a secondary line.
2. **Pseudonym legend**: a small legend (top bar / Queue "who is who" / ticket page) naming each
   pseudonym's role — sentinel, bot, author/story-owner — derived from the store's `actor_kind` and the
   sentinel set (prd/02-safe: roles only, never identities).

## Definition of done
The worklog view shows logged-vs-points (the r ≈ −0.05 flatness) and reframes the headline; the legend
names the sentinel/roles. Covered by node + backend tests.

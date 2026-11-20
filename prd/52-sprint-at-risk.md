# 52 — Sprint-at-risk forecast

## User journey
A lead asks "will sprint 3 (ends 27 Nov) land, and which stories are at risk?". The sprint lens shows
25/30 done, but doesn't say *which* of the 5 are at risk or why — DEL-167/168/183 (Jira-done but PR
unmerged), DEL-176 (stuck on Frank), DEL-161 (approved but never merged until the bot closed it).

## Main design
An "at risk" section for the active sprint: for each not-done-by-end story, classify the risk —
`done-but-unmerged` (Jira done, PR open), `stuck-on` (waiting on one person), `no-live-pr`, `approved-
unmerged` — with who owes each and the wait. Reuse the done-by-end/late/never machinery (prd/37) and the
"Done in Jira, PR open" / stale signals (prd/34, prd/42 M4). Surface it on the sprint strip and/or the
Overview.

## Definition of done
For the active sprint, the at-risk list names each open story, its risk reason, and who owes it
(DEL-167/168/183 = done-unmerged; DEL-176 = stuck on FrMo; DEL-161 = approved-unmerged). Covered by a
backend test + a node test.

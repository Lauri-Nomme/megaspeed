# 54 — Review SLA / chase action surface

## User journey
A lead has read "what's stuck" and wants the one action the data supports: **nudge the orphaned
approvals** (#55, #60, #52 waiting only on Frank; #53 on CaOs + Frank). The lab's best scenario is a
review SLA (~−9 %), but nothing in the UI turns that into "go chase these three today".

## Main design
A "what to do now" card (Overview) that names the few high-leverage actions derived from the data —
chase the orphaned approvals (with the PR keys and who owes), and/or adopt a review SLA — each linking
to the underlying items. This is a thin presentation over the existing stale/sentinel-tail/done-but-open
signals (prd/34, prd/37, prd/42), not new analytics.

## Definition of done
The card names #55/#60/#52 (+ #53) with who owes each and a "chase" affordance (links), and references
the lab's review-SLA scenario. Covered by a node test (card renders the orphans) + a backend test if a
new endpoint is needed.

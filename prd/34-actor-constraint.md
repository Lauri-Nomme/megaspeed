# 34 — Actor & constraint correctness (bots, sentinel, scope)

The product's headline "who is the constraint" is wrong: a bot is ranked #1, the sentinel is never
named as the owner, and the same actor reads differently across views. Conform to `docs/concepts.md`
§2 (bots / sentinel / constraint).

## Findings solved (docs/QA-exploratory.md)

- **#1** a bot (`BiBi`) is ranked the #1 constraint — charged 2,223 person-hours for 7 PRs it declined,
  and sets the "longest mean wait ≈13 d 5 h" headline. `actor_kind: bot` must be excluded from every
  person-level aggregate.
- **#2** zombie/stale PRs (37–90 days, bot-closed) = 43 % of all measured wait-hours and roughly double
  the sentinel's person-hours. Views must separate "idle until a bot closed it" from real review wait.
- **#8** cross-view contradictions: sentinel "overloaded" (Queue) vs "55 % busy" (Policy lab) vs "no
  rule" (Rules). Queue wait and calendar utilisation are different metrics and must be labelled as such.
- **#9** Delivery-list scope is inconsistent and dead-ends: `person:FrMo` → 0 items; `role:sentinel`
  silently ignored; `person:BoFe` → PRs *authored by* BoFe (Queue uses the same control for "waiting on").
- **#10** Overview "3 OVERLOADED PEOPLE · 5 slow" counts the synthetic `review` role as a person and
  drops a row (CaOs).
- **#23** Policy lab "WHO IS BUSY" lists `sentinel:1` and a 0 %-busy `FrMo` as two separate rows.

## Fix (conforms to concepts.md §2)

1. **Bots out of every person aggregate** — constraint ranking, "overloaded", person-hours, at-stake,
   and the scope select. Bot activity stays on the timeline; a bot-*closed* PR counts as "abandoned",
   not as a wait charged to a human (and not as a person-hour source).
2. **Name the owner** — when the ball is with the sentinel (or any single person), "waiting on" names
   them (`waiting on Frank`), never the generic "review queue".
3. **Scope semantics** — one meaning per view, stated: the Queue/Delivery person scope means "waiting on
   X", and `role:sentinel`/`role:review` work identically there (a PR waiting only on the sentinel shows
   up under `person:FrMo` *and* `role:sentinel`).
4. **Distinct, labelled load metrics** — "overloaded" (queue wait) and "busy/utilisation" (calendar
   share) get different names and are never both called "load"; a tile that counts people counts only
   people.
5. **Stale/zombie PR surfaced, and the tails not hidden** — a "stale PR" signal (open > N business
   days, or "ticket Done but PR open") is shown; the ball-court's `unobserved` tails (a PR with no
   recent events — e.g. the three 3–5-week-old waits #53/#55/#60 that the blocking graph currently
   drops) are surfaced in the blocking/delivery view rather than silently excluded, so "what's stuck
   now" doesn't omit the most actionable waits.

## Tests

- A bot is never a constraint/owner (synthetic store with a bot-declined batch → zero person-hours for
  the bot, the humans' numbers unchanged); a PR waiting only on the sentinel names the sentinel; the
  scope control's three shapes return the same set as the ball-court; a "stale PR" is flagged.

## Out of scope

The two new rules this implies — **stale-PR** and **sentinel-tail** — are in prd/37.

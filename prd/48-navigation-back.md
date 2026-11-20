# 48 — Navigation history & back

## User journey
A delivery lead opens Overview, clicks into the Delivery list, scopes to `person:FrMo`, then opens
`#ticket/DEL-167` and its PR #53 drawer. They hit the browser Back button expecting to retrace their
steps — instead they leave the app to the previous *site*. There is also no in-app Back button, so a
"where was I before this" step can only be done by hand.

## Main design
View/tenant/scope/timeframe changes today use `history.replaceState`, so the URL stays current but no
history entry is recorded; only ticket opens `pushState`. Switch navigation to `pushState` for each
distinct view state (view, tenant, scope, timeframe, sprint lens), so Back walks the in-app history.
Add a small Back affordance in the top bar that pops one in-app entry (and is disabled at the first
entry). Keep the deep-link URL contract (`?tenant=…&view=…&tf=…&scope=…` + `#ticket/KEY`) and the
existing "Back returns from a ticket to the page it opened from" behaviour. `popstate`/`hashchange`
must be idempotent (the prd/32 applyRoute already is).

## Definition of done
Back from a scoped view returns to the previous view (not the previous site); the in-app Back button
does the same; a fresh deep link still lands correctly; covered by node tests (history entries + Back
button). No regression to the shareable-URL or ticket-route behaviour.

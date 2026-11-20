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

**YAH ("You Are Here") bar** — a breadcrumb strip under the top bar showing the current location path
(`<tenant> · <view> · <ticket> · <PR>`), each segment clickable so the user can jump up the path (not
just one step back). It is the *location* (where am I), distinct from the Back button's *history* (where
was I); the two coexist. On phone it collapses to the nearest ancestors (tenant + view + current).

## Definition of done
Back from a scoped view returns to the previous view (not the previous site); the in-app Back button
does the same; the YAH bar shows the path and each segment jumps to that level; a fresh deep link still
lands correctly; covered by node tests (history entries + Back button + YAH segments). No regression to
the shareable-URL or ticket-route behaviour.

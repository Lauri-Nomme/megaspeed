# Papercuts — small UI/UX backlog

Trivial items, captured for a later polish pass. Each is small enough to be one commit.

## P-1 · Browser title reflects tenant + view

The browser tab title is static. It should read `<tenant> · <view> — megaspeed` (and `<tenant> · DEL-167 — megaspeed` on a ticket), updating when the tenant switcher or the view/ticket route changes. `document.title` is set once today; wire it to the router + tenant state (and the `#ticket/KEY` route already exists).

## P-2 · Shareable URL (deep link = full view state)

Copying the URL should reproduce the exact view for someone else: **tenant + view + selection**. Today `#ticket/DEL-167` survives reload, but the tenant and the active view are not in the URL (tenant is persisted client-side). Serialise the view state — tenant, view, any ticket/sprint/timeframe/scope selection — into the URL (query + hash), parse it on load, and update it as the user navigates, so `ctrl-c/ctrl-v` of the address bar lands a colleague in the same place. This also makes the tenant switcher and view changes shareable rather than session-local.

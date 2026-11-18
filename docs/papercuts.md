# Papercuts — small UI/UX backlog

Trivial items, captured for a later polish pass. Each is small enough to be one commit.

## P-1 · Browser title reflects tenant + view

The browser tab title is static. It should read `<tenant> · <view> — megaspeed` (and `<tenant> · DEL-167 — megaspeed` on a ticket), updating when the tenant switcher or the view/ticket route changes. `document.title` is set once today; wire it to the router + tenant state (and the `#ticket/KEY` route already exists).

**Done.** `app/route.js:pageTitle()` builds the title; `main.js:syncTitle()` runs on every `renderPage()` and whenever the tenant switcher repaints. No tenant yet → `Timeline — megaspeed` (no dangling separator).

## P-2 · Shareable URL (deep link = full view state)

Copying the URL should reproduce the exact view for someone else: **tenant + view + selection**. Today `#ticket/DEL-167` survives reload, but the tenant and the active view are not in the URL (tenant is persisted client-side). Serialise the view state — tenant, view, any ticket/sprint/timeframe/scope selection — into the URL (query + hash), parse it on load, and update it as the user navigates, so `ctrl-c/ctrl-v` of the address bar lands a colleague in the same place. This also makes the tenant switcher and view changes shareable rather than session-local.

**Done.** The address bar is the view state:

```
/?tenant=del-combined&view=timeline&tf=1q&scope=all
/?tenant=del-combined&view=ticket&tf=1q&scope=person%3AAbCd#ticket/DEL-167
```

`app/route.js` serialises/parses it (tenant, view, ticket, timeframe, scope); `main.js:syncUrl()` `replaceState`s on every navigation and `pushState`s only when a ticket opens, so Back returns from a ticket to the page it was opened from. A bare `#ticket/KEY` still resolves. Covered by `app/test/route.test.mjs`.

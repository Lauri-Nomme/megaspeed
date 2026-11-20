/* prd/48 — the in-app navigation history (the Back stack).
 *
 * Before this, the address bar followed the view with `replaceState`, so the
 * URL was current but no entry was recorded: the browser's Back button left
 * the app for the previous *site* (prd/48 user journey). Now every distinct
 * view state — view, tenant, scope, timeframe, sprint lens — is a
 * `pushState` entry, so Back walks the app's own steps. `replaceState` is
 * still used for the states that only *refine* the entry the user is already
 * on: a sprint's measured `from`/`to`, a theme, and the canonical spelling of
 * a URL the browser restored.
 *
 * This module is the bookkeeping half: a pure stack of `{ href, key }`
 * entries and the decision of what the address bar should do next. It touches
 * no `window`, no DOM and no History API, so `node --test` can walk a
 * journey (and a Back) without a browser. `main.js` mirrors each decision
 * into the real History API.
 */

/**
 * The identity of a view state: the fields that make a navigation a *step*.
 *
 * `from`/`to` only refine a sprint lens and `theme` is a skin, so neither is a
 * step of its own — the same view with a resolved sprint window keeps the
 * entry it already has.
 */
export function routeKey({
  tenant = null, view = null, ticket = null, timeframe = null,
  scope = null, sprint = null,
} = {}) {
  const scopeKey = scope && scope.kind ? `${scope.kind}:${scope.value == null ? '' : scope.value}` : '';
  return JSON.stringify([
    tenant || null,
    view || null,
    ticket || null,
    timeframe || null,
    scopeKey || null,
    sprint == null || sprint === '' ? null : String(sprint),
  ]);
}

function entry(href, key) {
  return { href: href == null ? '' : String(href), key: key == null ? '' : String(key) };
}

function copy(item) {
  return item ? { href: item.href, key: item.key } : null;
}

/**
 * A pure in-app entry stack. `index` is the entry the user is on; entries
 * after it are the forward steps the browser still holds (a fresh `push`
 * drops them, exactly as the browser does).
 *
 * `seed` and `restore` accept the URL the browser actually moved to, so the
 * stack never claims a step the app did not record: after a reload the stack
 * is one entry deep and the in-app Back is honestly disabled.
 */
export function createHistory(seed = null) {
  const stack = [];
  let at = -1;

  const api = {
    get depth() { return stack.length; },
    get index() { return at; },
    /** 1-based position, like "step 2 of 5". */
    get position() { return at + 1; },
    entries() { return stack.map(copy); },
    current() { return copy(at >= 0 ? stack[at] : null); },
    previous() { return copy(at > 0 ? stack[at - 1] : null); },
    /** True while the user has an in-app step to go back to. */
    canBack() { return at > 0; },

    /** The URL the page loaded with is step 0, not a navigation. */
    seed(href, key) {
      stack.length = 0;
      stack.push(entry(href, key));
      at = 0;
      return api.current();
    },

    /** Record a new step; the forward entries are gone (the browser agrees). */
    push(href, key) {
      stack.length = at + 1;
      stack.push(entry(href, key));
      at = stack.length - 1;
      return at;
    },

    /** The current entry changed its address (or its canonical spelling). */
    replace(href, key) {
      if (at < 0) return api.seed(href, key);
      stack[at] = entry(href, key);
      return api.current();
    },

    /** Pop one entry — the pure half of the in-app Back button. */
    back() {
      if (!api.canBack()) return null;
      at -= 1;
      return api.current();
    },

    /** Pop the other way, for completeness (browser Forward). */
    forward() {
      if (at >= stack.length - 1) return null;
      at += 1;
      return api.current();
    },

    /**
     * The browser moved to an entry (popstate / hashchange / a hand-edited
     * URL). A known index wins; an unknown entry (a page from before a reload,
     * or a URL the app never wrote) becomes the only entry — we cannot claim
     * steps we did not record.
     */
    restore(index, href = '', key = '') {
      if (Number.isInteger(index) && index >= 0 && index < stack.length) {
        at = index;
        stack[at] = entry(href || stack[at].href, key || stack[at].key);
        return api.current();
      }
      if (href) return api.seed(href, key);
      if (at >= stack.length) at = stack.length - 1;
      if (at < 0 && stack.length) at = 0;
      return api.current();
    },
  };

  if (seed && seed.href != null) api.seed(seed.href, seed.key);
  return api;
}

/**
 * Decide what the address bar should do for the view state now on screen.
 *
 *   push     a new step (view, tenant, scope, timeframe, sprint lens)
 *   replace  the same step with a sharper URL (a sprint's `from`/`to`, a theme)
 *   keep     the URL already says this; only the stack's entry is recorded
 *   restore  a history walk rewrote the bar; never a new step
 *
 * `href` is the canonical URL for the state, `current` is what the address bar
 * holds, and `key` is `routeKey()` of the state. `restoring` marks the
 * popstate/hashchange/boot path, where the browser is the one that moved.
 */
export function planSync(nav, { href, current, key, restoring = false }) {
  const target = href == null ? '' : String(href);
  const here = key == null ? '' : String(key);

  if (restoring) {
    // The browser already holds this entry; only its spelling may need fixing.
    nav.replace(target, here);
    return { action: target === current ? 'keep' : 'replace', index: nav.index, href: target };
  }

  if (target === current) {
    // The bar and the state agree: record the state on the entry it belongs to.
    if (!nav.current()) nav.seed(target, here);
    else nav.replace(target, here);
    return { action: 'keep', index: nav.index, href: target };
  }

  const top = nav.current();
  if (!top || top.key !== here) {
    return { action: 'push', index: nav.push(target, here), href: target };
  }
  // Same step, sharper URL (a measured sprint window, the canonical link).
  nav.replace(target, here);
  return { action: 'replace', index: nav.index, href: target };
}

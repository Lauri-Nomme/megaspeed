/* prd/48 — navigation history & Back, plus the YAH ("You Are Here") bar.
 *
 * The two halves are pure data, so the journey from the PRD is walked here
 * without a browser: Overview → Delivery → scoped to a person → DEL-167, then
 * Back twice, landing on the scope and then the view. The last test guards the
 * page wiring (the Back button, the strip, the `pushState` call sites).
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { createHistory, planSync, routeKey } from '../history.js';
import { collapseYah, yahPath } from '../yah.js';
import { parseRoute, serializeRoute } from '../route.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/** `?query#hash` → the two arguments `location` would give the parser. */
function split(href) {
  const at = href.indexOf('#');
  return at < 0
    ? { search: href, hash: '' }
    : { search: href.slice(0, at), hash: href.slice(at) };
}

/** The URL the address bar holds for a view state. */
function hrefOf(route) {
  return `/${serializeRoute(route)}`;
}

/** Parse a full `/?query#hash` URL the way `applyRoute` does. */
function routeOf(href) {
  const { search, hash } = split(href.startsWith('/') ? href.slice(1) : href);
  return parseRoute(search, hash);
}

/* ---- the identity of a step -------------------------------------------- */

test('prd/48: a step is view/tenant/scope/timeframe/sprint — not its window or theme', () => {
  const base = {
    tenant: 'del-combined', view: 'timeline', timeframe: '1q',
    scope: { kind: 'person', value: 'FrMo' }, sprint: 2,
  };
  assert.equal(routeKey(base), routeKey({ ...base }));
  // a sprint's measured window and the theme refine the entry, they are not steps
  assert.equal(routeKey({ ...base, from: 1, to: 2, theme: 'light' }), routeKey(base));
  // every navigation dimension is a step of its own
  for (const change of [
    { tenant: 'del' },
    { view: 'delivery' },
    { ticket: 'DEL-167' },
    { timeframe: '4w' },
    { scope: { kind: 'person', value: 'AbCd' } },
    { scope: { kind: 'all', value: null } },
    { sprint: 3 },
  ]) {
    assert.notEqual(routeKey({ ...base, ...change }), routeKey(base), JSON.stringify(change));
  }
});

/* ---- the PRD journey: Overview → Delivery → scoped → ticket ------------ */

test('prd/48: a scoped view is a history entry, so Back returns to the previous view', () => {
  const nav = createHistory();
  const overview = { tenant: 'del-combined', view: 'overview', timeframe: '1q', scope: { kind: 'all', value: null } };
  const delivery = { ...overview, view: 'delivery' };
  const scoped = { ...delivery, scope: { kind: 'person', value: 'FrMo' } };

  // boot: the URL the window opened with is step 0, never a navigation
  const first = hrefOf(overview);
  nav.seed(first, routeKey(overview));
  assert.equal(nav.canBack(), false);

  const toDelivery = planSync(nav, {
    href: hrefOf(delivery), current: first, key: routeKey(delivery),
  });
  assert.equal(toDelivery.action, 'push');
  assert.equal(nav.depth, 2);

  // scoping the list to a person is a step of its own
  const toScoped = planSync(nav, {
    href: hrefOf(scoped), current: hrefOf(delivery), key: routeKey(scoped),
  });
  assert.equal(toScoped.action, 'push');
  assert.equal(nav.depth, 3);
  assert.equal(nav.index, 2);

  // the browser Back button: popstate carries the entry's index and URL
  const restored = nav.restore(1, hrefOf(delivery), routeKey(delivery));
  assert.equal(restored.key, routeKey(delivery));
  assert.equal(nav.index, 1);
  // the previous view is what the scoped entry replaced on screen
  const backTo = routeOf(restored.href);
  assert.equal(backTo.view, 'delivery');
  assert.deepEqual(backTo.scope, { kind: 'all', value: null, label: 'everything' });
  // ... and another Back still reaches Overview: history was recorded, not overwritten
  assert.equal(nav.canBack(), true);
  assert.equal(nav.back().href, first);
  assert.equal(nav.canBack(), false);

  // a scope change on one view is a distinct entry, not a rewrite of it
  assert.notEqual(toScoped.index, toDelivery.index);
});

test('prd/48: a ticket is a step, and leaving it lands back on the page it opened from', () => {
  const nav = createHistory();
  const delivery = { tenant: 'del-combined', view: 'delivery', timeframe: '1q', scope: { kind: 'person', value: 'FrMo' } };
  const ticket = { ...delivery, view: 'ticket', ticket: 'DEL-167' };

  nav.seed(hrefOf(delivery), routeKey(delivery));
  const opened = planSync(nav, {
    href: hrefOf(ticket), current: hrefOf(delivery), key: routeKey(ticket),
  });
  assert.equal(opened.action, 'push');

  // the deep-link contract survives on the ticket entry
  const route = routeOf(hrefOf(ticket));
  assert.equal(route.tenant, 'del-combined');
  assert.equal(route.view, 'ticket');
  assert.equal(route.ticket, 'DEL-167');
  assert.deepEqual(route.scope, { kind: 'person', value: 'FrMo', label: 'FrMo' });

  // Back from the ticket is the delivery list, scope intact
  const back = nav.back();
  const previous = routeOf(back.href);
  assert.equal(previous.view, 'delivery');
  assert.equal(previous.ticket, null);
  assert.deepEqual(previous.scope, { kind: 'person', value: 'FrMo', label: 'FrMo' });
});

/* ---- push vs replace vs restore ---------------------------------------- */

test('prd/48: a refinement of the same step replaces instead of stacking entries', () => {
  const nav = createHistory();
  const lens = { tenant: 'del-combined', view: 'timeline', timeframe: 'all', sprint: 2 };
  nav.seed(hrefOf(lens), routeKey(lens));

  // the sprint window is measured asynchronously: same step, sharper URL
  const measured = { ...lens, from: 1757562000000, to: 1759824000000 };
  const plan = planSync(nav, {
    href: hrefOf(measured), current: hrefOf(lens), key: routeKey(measured),
  });
  assert.equal(plan.action, 'replace');
  assert.equal(nav.depth, 1);
  assert.equal(nav.canBack(), false);

  // a different sprint lens *is* a step
  const other = { ...lens, sprint: 3 };
  const next = planSync(nav, {
    href: hrefOf(other), current: hrefOf(measured), key: routeKey(other),
  });
  assert.equal(next.action, 'push');
  assert.equal(nav.depth, 2);
});

test('prd/48: a restore never pushes — Back followed by its hashchange is idempotent', () => {
  const nav = createHistory();
  const a = { tenant: 'del-combined', view: 'overview', timeframe: '1q' };
  const b = { tenant: 'del-combined', view: 'timeline', timeframe: '1q' };
  nav.seed(hrefOf(a), routeKey(a));
  planSync(nav, { href: hrefOf(b), current: hrefOf(a), key: routeKey(b) });
  assert.equal(nav.depth, 2);

  // popstate: the browser is already on entry 0; applyRoute renders it again
  nav.restore(0, hrefOf(a), routeKey(a));
  const first = planSync(nav, {
    href: hrefOf(a), current: hrefOf(a), key: routeKey(a), restoring: true,
  });
  assert.equal(first.action, 'keep');
  const second = planSync(nav, {
    href: hrefOf(a), current: hrefOf(a), key: routeKey(a), restoring: true,
  });
  assert.equal(second.action, 'keep');
  assert.equal(nav.depth, 2, 'two restores of one entry still added no entry');
  assert.equal(nav.index, 0);
  assert.equal(nav.canBack(), false);
});

/* ---- the in-app Back button -------------------------------------------- */

test('prd/48: the in-app Back button pops one entry and is disabled at the first', () => {
  const nav = createHistory();
  const overview = { tenant: 'del-combined', view: 'overview', timeframe: '1q' };
  const delivery = { ...overview, view: 'delivery' };
  const scoped = { ...delivery, scope: { kind: 'role', value: 'review' } };

  nav.seed(hrefOf(overview), routeKey(overview));
  // first entry: the control is disabled (renderBack reads canBack)
  assert.equal(nav.canBack(), false);
  assert.equal(nav.back(), null);

  planSync(nav, { href: hrefOf(delivery), current: hrefOf(overview), key: routeKey(delivery) });
  planSync(nav, { href: hrefOf(scoped), current: hrefOf(delivery), key: routeKey(scoped) });
  assert.equal(nav.canBack(), true);
  assert.equal(nav.depth, 3);

  // one press = one entry
  assert.equal(nav.back().key, routeKey(delivery));
  assert.equal(nav.depth, 3, 'Back pops the position, not the recorded entries');
  assert.equal(nav.index, 1);
  assert.equal(nav.back().key, routeKey(overview));
  assert.equal(nav.canBack(), false);
  assert.equal(nav.back(), null);

  // a new step after Back drops the forward entries, like the browser does
  nav.push(hrefOf(delivery), routeKey(delivery));
  assert.equal(nav.depth, 2);
  assert.equal(nav.forward(), null);
});

/* ---- the YAH bar ------------------------------------------------------- */

test('prd/48: the YAH bar shows tenant · view · ticket · PR, and each level is a jump', () => {
  const steps = yahPath({
    tenant: 'del-combined',
    view: 'delivery',
    viewLabel: 'Delivery list',
    ticket: 'DEL-167',
    pr: { number: 53, repo: 'DEL/delivery-api' },
    timeframe: '1q',
    scope: { kind: 'person', value: 'FrMo' },
  });
  assert.deepEqual(steps.map((step) => step.level), ['tenant', 'view', 'ticket', 'pr']);
  assert.deepEqual(steps.map((step) => step.label), ['del-combined', 'Delivery list', 'DEL-167', '#53']);

  // the tenant level is the dataset's front page (a scope is per dataset)
  const tenant = routeOf(steps[0].href);
  assert.equal(tenant.tenant, 'del-combined');
  assert.equal(tenant.view, 'overview');
  assert.equal(tenant.scope, null);

  // the view level keeps the lens but drops the ticket (and the drawer)
  const view = routeOf(steps[1].href);
  assert.equal(view.view, 'delivery');
  assert.equal(view.ticket, null);
  assert.deepEqual(view.scope, { kind: 'person', value: 'FrMo', label: 'FrMo' });

  // the ticket level is the ticket route; the PR level sits on it (a drawer has no URL)
  const ticket = routeOf(steps[2].href);
  assert.equal(ticket.view, 'ticket');
  assert.equal(ticket.ticket, 'DEL-167');
  assert.equal(steps[3].href, steps[2].href);
});

test('prd/48: the phone keeps the nearest ancestors, the desktop keeps the path', () => {
  const steps = yahPath({
    tenant: 'del-combined', view: 'delivery', viewLabel: 'Delivery list',
    ticket: 'DEL-167', pr: { number: 53 },
  });
  assert.equal(steps.length, 4);
  assert.deepEqual(collapseYah(steps).map((step) => step.level), ['tenant', 'view', 'pr']);
  assert.deepEqual(collapseYah(steps).map((step) => step.label), ['del-combined', 'Delivery list', '#53']);
  // a short path is already at the phone size
  assert.equal(collapseYah(steps.slice(0, 2)).length, 2);
  assert.equal(collapseYah([]).length, 0);
});

test('prd/48: a fresh deep link lands as the first step, with nothing to pop', () => {
  const href = '?tenant=del-combined&view=ticket&tf=1q&scope=person%3AFrMo#ticket/DEL-167';
  const route = routeOf(href);
  const nav = createHistory();
  // boot seeds the URL the browser already holds (seedHistory)
  nav.seed(href, routeKey(route));

  const plan = planSync(nav, { href: `/${href}`, current: `/${href}`, key: routeKey(route) });
  assert.equal(plan.action, 'keep');
  assert.equal(nav.canBack(), false);
  assert.equal(nav.depth, 1);
  // ... and the link still names the same place
  assert.equal(route.tenant, 'del-combined');
  assert.equal(route.view, 'ticket');
  assert.equal(route.ticket, 'DEL-167');
  assert.deepEqual(route.scope, { kind: 'person', value: 'FrMo', label: 'FrMo' });
});

/* ---- page wiring guard ------------------------------------------------- */

test('prd/48: the page wires the Back stack, the Back button and the YAH bar', () => {
  const html = read('index.html');
  assert.match(html, /id="history-back"/);
  assert.match(html, /class="history-back"/);
  assert.match(html, /id="yah"/);
  assert.match(html, /id="yah-path"/);

  const main = read('main.js');
  // the stack and the decision live in history.js; the page mirrors them
  assert.match(main, /const nav = createHistory\(\)/);
  assert.match(main, /planSync\(nav, \{/);
  assert.match(main, /history\.pushState\(\{ ms: plan\.index \}, '', plan\.href\)/);
  assert.match(main, /history\.replaceState\(\{ ms: plan\.index \}, '', plan\.href\)/);
  // boot seeds step 0; the render follows
  assert.match(main, /seedHistory\(\);\nrenderPage\(\);/);
  // the in-app Back pops the browser's entry and is disabled at the first
  assert.match(main, /function inAppBack\(\)/);
  assert.match(main, /history\.back\(\)/);
  assert.match(main, /button\.disabled = !can/);
  // a restore is flagged; it can never push a duplicate entry
  assert.match(main, /restoringRoute = true/);
  assert.match(main, /window\.addEventListener\('popstate', \(event\) => \{\n  restoreFromLocation\(event\);\n  applyRoute\(\);/);
  // the YAH bar renders from the path and jumps by level
  assert.match(main, /function renderYah\(\)/);
  assert.match(main, /collapseYah\(steps\)/);
  assert.match(main, /data-yah="\$\{esc\(step\.level\)\}"/);
  assert.match(main, /function yahJump\(level\)/);
  assert.match(main, /syncTitle\(\);\n  syncUrl\(\);\n  renderYah\(\);/);

  const css = read('style.css');
  assert.match(css, /\.yah \{[^}]*position: sticky/s);
  // the phone collapses the strip and stops sticking it
  const phone = css.slice(css.indexOf('@media (max-width: 640px)'));
  assert.match(phone, /\.yah \{ position: static;/);
  assert.match(phone, /\.yah-k \{ display: none;/);
  assert.match(phone, /\.history-back \{ order: 2;/);
});

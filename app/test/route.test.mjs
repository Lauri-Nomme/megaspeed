/* Shareable view state: the tab title (P-1) and the deep-link URL (P-2).
 *
 * The scheme is pure data, so the whole round trip is asserted without a
 * browser: serialise a view state, parse it back, and the same tenant + view +
 * ticket (plus timeframe and scope) come out. The last test guards the wiring
 * in main.js — the page must actually call these helpers, replace the URL on
 * navigation, and still honour a bare `#ticket/KEY`.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  FALLBACK_TITLE, ROUTE_VIEWS, pageTitle, parseRoute, parseScope,
  serializeRoute, ticketFromHash,
} from '../route.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/** `?query#hash` → the two arguments `location` would give the parser. */
function split(href) {
  const at = href.indexOf('#');
  return at < 0
    ? { search: href, hash: '' }
    : { search: href.slice(0, at), hash: href.slice(at) };
}

test('P-1: the tab title names the tenant and the view', () => {
  assert.equal(
    pageTitle({ tenant: 'del-combined', view: 'timeline', title: 'Timeline' }),
    'del-combined · Timeline — megaspeed',
  );
  // every view uses its own title from PAGES
  assert.equal(
    pageTitle({ tenant: 'del-combined', view: 'delivery', title: 'Delivery list' }),
    'del-combined · Delivery list — megaspeed',
  );
  // a ticket names the key, not the word "Ticket"
  assert.equal(
    pageTitle({ tenant: 'del-combined', view: 'ticket', ticket: 'DEL-167', title: 'Ticket' }),
    'del-combined · DEL-167 — megaspeed',
  );
});

test('P-1: no tenant yet is still a correct title', () => {
  assert.equal(pageTitle({ view: 'timeline', title: 'Timeline' }), 'Timeline — megaspeed');
  assert.equal(pageTitle({ view: 'ticket', ticket: 'DEL-167', title: 'Ticket' }), 'DEL-167 — megaspeed');
  assert.equal(pageTitle({}), FALLBACK_TITLE);
});

test('P-2: the URL round-trips tenant + view + ticket', () => {
  const view = {
    tenant: 'del-combined',
    view: 'timeline',
    ticket: null,
    timeframe: '4w',
    scope: { kind: 'person', value: 'AbCd', label: 'AbCd' },
  };
  const href = serializeRoute(view);
  assert.equal(href, '?tenant=del-combined&view=timeline&tf=4w&scope=person%3AAbCd');
  const { search, hash } = split(href);
  const parsed = parseRoute(search, hash);
  assert.equal(parsed.tenant, view.tenant);
  assert.equal(parsed.view, view.view);
  assert.equal(parsed.ticket, view.ticket);
  assert.equal(parsed.timeframe, view.timeframe);
  assert.deepEqual(parsed.scope, view.scope);

  // ... and with a ticket the hash carries the key, the query the rest
  const ticketHref = serializeRoute({ ...view, view: 'ticket', ticket: 'DEL-167' });
  assert.equal(
    ticketHref,
    '?tenant=del-combined&view=ticket&tf=4w&scope=person%3AAbCd#ticket/DEL-167',
  );
  const ticketParts = split(ticketHref);
  const ticketRoute = parseRoute(ticketParts.search, ticketParts.hash);
  assert.equal(ticketRoute.tenant, 'del-combined');
  assert.equal(ticketRoute.view, 'ticket');
  assert.equal(ticketRoute.ticket, 'DEL-167');
});

test('P-2: #ticket/KEY still resolves, alone and inside the new scheme', () => {
  assert.equal(ticketFromHash('#ticket/DEL-167'), 'DEL-167');
  assert.equal(ticketFromHash('#ticket/'), null);
  assert.equal(ticketFromHash(''), null);
  assert.equal(ticketFromHash('#ticket/%E0%A4%A'), null); // malformed escape

  const legacy = parseRoute('', '#ticket/DEL-167');
  assert.equal(legacy.tenant, null);
  assert.equal(legacy.view, 'ticket');
  assert.equal(legacy.ticket, 'DEL-167');
  // the hash wins over a stale `view`, so an old link cannot land on a list
  const both = parseRoute('?tenant=del&view=timeline', '#ticket/ENG-9');
  assert.equal(both.view, 'ticket');
  assert.equal(both.ticket, 'ENG-9');
  assert.equal(both.tenant, 'del');
});

test('P-2: unknown or absent parameters are ignored, not trusted', () => {
  assert.deepEqual(parseRoute('', ''), {
    tenant: null, view: null, ticket: null, timeframe: null, scope: null,
  });
  assert.equal(parseRoute('?view=bogus', '').view, null);
  assert.equal(parseRoute('?tenant=del', '').view, null);
  // the tenant is decoded like any query value (and re-encoded on the way out)
  assert.equal(parseRoute('?tenant=my+data+%26+co', '').tenant, 'my data & co');
  assert.equal(parseRoute(serializeRoute({ tenant: 'my data & co' }), '').tenant, 'my data & co');
  // scope: explicit "all" reproduces "everything"; absent means "keep mine"
  assert.deepEqual(parseScope('all'), { kind: 'all', value: null, label: 'everything' });
  assert.equal(parseScope(''), null);
  assert.equal(parseScope('person:'), null);
  assert.equal(parseScope('bogus:value'), null);
  assert.equal(serializeRoute({ scope: { kind: 'all' } }), '?scope=all');
});

test('P-2: the page wires the title, the URL and the ticket route', () => {
  const main = read('main.js');
  // P-1: the title is derived from state, not set once
  assert.match(main, /document\.title = pageTitle\(/);
  assert.match(main, /function syncTitle\(\)/);
  assert.match(main, /syncTitle\(\);\n  syncUrl\(\);/); // both follow renderPage
  // P-2: serialise on every render, replace (never reload), push only a ticket
  assert.match(main, /function syncUrl\(\{ push = false \} = \{\}\)/);
  assert.match(main, /history\.replaceState\(null, '', href\)/);
  assert.match(main, /history\.pushState\(null, '', href\)/);
  assert.match(main, /function routeHref\(\)/);
  assert.match(main, /serializeRoute\(\{/);
  assert.match(main, /parseRoute\(location\.search, location\.hash\)/);
  // the ticket route is part of the same scheme, not a special case
  assert.match(main, /#ticket\//);
  assert.match(main, /ticketFromHash/);
  assert.match(main, /addEventListener\('hashchange'/);
  assert.match(main, /addEventListener\('popstate'/);
  // the phone drawer still navigates through showPage, which syncs the URL
  assert.match(main, /await showPage\(nav\.dataset\.page\)/);
  assert.match(main, /phoneEntryPage\(null\)/);
  // every known view has a title to print
  assert.ok(ROUTE_VIEWS.includes('ticket'));
  assert.ok(ROUTE_VIEWS.includes('sources'));
});

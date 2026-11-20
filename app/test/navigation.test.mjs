/* prd/38 navigation & papercuts — the pure markup/route decisions.
 *
 * The dead ends the exploratory run found were structural: a PR row with no
 * handler, a Timeline chip that scoped to the author, a landing view nobody
 * chose, a "+N more" that did not exist, and a ticket key only reachable from a
 * Rules drill-down. These tests pin the fixed behaviour without a browser; the
 * wiring guards at the end keep the page in step.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { deliveryTable, overviewMarkup, matchesScope } from '../tables.js';
import { prTable } from '../ticket.js';
import { ticketFromQuery } from '../tickets.js';
import {
  filteredPages, scopeBanner, scopeMeaning,
} from '../scope.js';
import { SCOPE_KINDS, parseRoute, serializeRoute } from '../route.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/* ---- #12: a ticket's PR row is a doorway ------------------------------- */

function ticketPayload() {
  return {
    key: 'DEL-155',
    prs: [{
      object_id: 'PR:DEL/delivery-api#53', number: 53, repo: 'DEL/delivery-api',
      state: 'open', author: 'AlNg', opened: '2026-10-14T16:10:36Z',
      cycle_hours: 184, censored: true, review_rounds: 2,
      push_classes: { content: 2 }, commit_count: 7, intervals: [],
    }],
  };
}

test('a ticket PR row is a labelled, keyboard-reachable drawer target', () => {
  const html = prTable(ticketPayload());
  assert.match(html, /class="pr-row" data-pr="PR:DEL\/delivery-api#53"/);
  assert.match(html, /tabindex="0"/);
  assert.match(html, /role="button"/);
  assert.match(html, /title="open PR #53 delivery-api in the drawer"/);
  assert.match(html, /aria-label="open PR #53 delivery-api in the drawer"/);
  // the ticket key travels with the row, so the drawer knows its story
  assert.match(html, /data-pr-ticket="DEL-155"/);
});

test('a delivery row carries the same drawer target', () => {
  const row = {
    object_id: 'PR:DEL/delivery-api#53', id: 'delivery-api#53', number: 53,
    repo_short: 'delivery-api', tickets: ['DEL-155'], author: 'AlNg',
    state: 'open', ball: 'review', ball_label: 'review', phase: 'review',
    waiting_since: '2026-11-13T12:07:00Z', waiting_hours: 2.9, age_hours: 16.4,
    blocked: true, dependents: 0, impact_hours: 0, commits: 2,
  };
  const html = deliveryTable({ rows: [row] }, 'all');
  assert.match(html, /data-id="PR:DEL\/delivery-api#53"/);
  assert.match(html, /data-pr="PR:DEL\/delivery-api#53"/);
  assert.match(html, /title="open PR #53 delivery-api in the drawer"/);
  assert.match(html, /role="button"/);
});

/* ---- #25: the needs-attention list says how many it hid ---------------- */

function deliveryRows(count) {
  return Array.from({ length: count }, (_, index) => ({
    object_id: `PR:DEL/web#${index + 1}`, number: index + 1,
    repo_short: 'web', tickets: [`DEL-${100 + index}`], author: 'AlNg',
    state: 'open', ball: 'review', ball_label: 'review', phase: 'review',
    waiting_since: '2026-11-13T12:07:00Z', waiting_hours: 90 + index,
    age_hours: 200, blocked: false, dependents: 0, impact_hours: 0,
    commits: 2,
  }));
}

test('the needs-attention list shows +N more when truncated', () => {
  const html = overviewMarkup({
    delivery: { counts: { open: 8, blocked: 0, oldest_waiting: 8 }, rows: deliveryRows(8) },
    queues: null,
    blocking: null,
  });
  assert.match(html, /\+2 more open items/);
  assert.match(html, /data-go="delivery"/);
  // four rows fit, so there is no +N hint
  const short = overviewMarkup({
    delivery: { counts: { open: 4, blocked: 0, oldest_waiting: 4 }, rows: deliveryRows(4) },
    queues: null,
    blocking: null,
  });
  assert.doesNotMatch(short, /more open item/);
});

/* ---- the ticket search box --------------------------------------------- */

test('the go-to-ticket box resolves the forms people paste', () => {
  assert.equal(ticketFromQuery('DEL-155'), 'DEL-155');
  assert.equal(ticketFromQuery('  del-155 '), 'DEL-155');
  assert.equal(ticketFromQuery('STORY:DEL-155'), 'DEL-155');
  assert.equal(ticketFromQuery('TICKET:DEL-155'), 'DEL-155');
  assert.equal(ticketFromQuery('#ticket/DEL-155'), 'DEL-155');
  assert.equal(ticketFromQuery('http://127.0.0.1:8766/?tenant=del#ticket/DEL-155'), 'DEL-155');
  assert.equal(ticketFromQuery('#ticket/DEL%2D155'), 'DEL-155');
  // not a key: a sentence, a bare number, an empty box
  assert.equal(ticketFromQuery('DEL-155 and DEL-156'), '');
  assert.equal(ticketFromQuery('155'), '');
  assert.equal(ticketFromQuery(''), '');
});

/* ---- #20: the Timeline chip scopes to the ticket, not the author -------- */

test('a ticket scope narrows the timeline and the delivery list', () => {
  assert.ok(SCOPE_KINDS.includes('ticket'));
  const scope = { kind: 'ticket', value: 'DEL-155', label: 'DEL-155' };
  const pages = filteredPages(scope);
  assert.ok(pages.has('timeline'));
  assert.ok(pages.has('delivery'));
  assert.equal(pages.has('queue'), false);
  assert.match(scopeMeaning(scope, 'delivery'), /open PRs that name DEL-155/);
  assert.match(scopeMeaning(scope, 'timeline'), /events for DEL-155/);
  const banner = scopeBanner(scope, 'timeline');
  assert.match(banner, /Scope ticket DEL-155/);
  assert.match(banner, /filtered to it/);
  assert.match(banner, /not filtered/);
});

test('a ticket scope round-trips through the shareable URL', () => {
  const route = parseRoute('?tenant=del&view=timeline&scope=ticket%3ADEL-155');
  assert.deepEqual(route.scope, { kind: 'ticket', value: 'DEL-155', label: 'DEL-155' });
  assert.match(
    serializeRoute({ view: 'timeline', scope: { kind: 'ticket', value: 'DEL-155' } }),
    /scope=ticket%3ADEL-155/,
  );
});

test('a ticket scope matches the rows that name the ticket', () => {
  const scope = { kind: 'ticket', value: 'del-155' };
  assert.equal(matchesScope({ tickets: ['DEL-1', 'DEL-155'] }, scope), true);
  assert.equal(matchesScope({ tickets: ['DEL-156'] }, scope), false);
  // the story ticket is enough even when the shared epic key is also present
  assert.equal(matchesScope({ tickets: ['DEL-1'], story_tickets: ['DEL-155'] }, scope), true);
});

/* ---- source guards: the page wiring ------------------------------------ */

test('prd/38: the page lands on Overview and closes the drawer on a view change', () => {
  const main = read('main.js');
  assert.match(main, /page: 'overview'/);
  assert.match(main, /if \(page !== state\.page\) closeDrawer\(\)/);
  // a ticket opened from another view also closes it
  assert.match(main, /state\.ticketFrom = state\.page;\n    closeDrawer\(\);/);
  // phone entry is the same front door (mobile.js owns the helper)
  assert.match(read('mobile.js'), /return savedPage \|\| 'overview';/);
});

test('prd/38: the top bar has the ticket search and the page wires it', () => {
  const html = read('index.html');
  assert.match(html, /id="ticket-jump"/);
  assert.match(html, /id="ticket-jump-input"/);
  const main = read('main.js');
  assert.match(main, /ticketFromQuery\(input\.value\)/);
  assert.match(main, /await openTicket\(key\)/);
});

test('prd/38 #18: Sources uses the ingest run figures and one snapshot age', () => {
  const main = read('main.js');
  assert.match(main, /function snapshotAge\(\)/);
  assert.match(main, /meta\.prs_kept/);
  assert.match(main, /meta\.prs_seen/);
  assert.match(main, /Snapshot from \$\{snapshotAge\(\)\}/);
  assert.match(main, /\$\{fmtStamp\(Date\.parse\(meta\.as_of\)\)\} \(\$\{snapshotAge\(\)\}\)/);
});

test('regression: a desktop delivery row opens the inspector, not the PR drawer', () => {
  // The delivery list has its own inline detail panel, so a row click must not
  // also open the drawer — that stacked two panels for the same PR. Only the
  // overview (no inspector) opens the drawer from a row.
  const main = read('main.js');
  assert.doesNotMatch(main, /state\.page === 'delivery' && !isPhone\(\)/);
  assert.match(main, /state\.page === 'overview'\) \{\s*openDeliveryPrDrawer/);
});

test('prd/38 #20 / prd/43 #3: the Timeline chip scopes to the ticket and keeps the PR marked', () => {
  const main = read('main.js');
  // prd/43 §3: one helper reads the ticket from the entity, the PR or the row
  assert.match(main, /drawerTimelineScope\(entity\)/);
  assert.match(main, /setScope\(scope\.kind, scope\.value/);
  assert.match(main, /timeline\.focusPr\(prId\)/);
  assert.match(read('timeline.js'), /focusPr\(id\)/);
  assert.match(read('timeline.js'), /this\.focusPrId === pr\.id/);
});

test('P-3: the build version is a fixed bottom-left footer, not the meta strip', () => {
  const html = read('index.html');
  const footer = html.slice(html.indexOf('<footer class="app-footer"'));
  assert.match(footer, /id="app-version"/);
  const meta = html.slice(html.indexOf('<div class="meta"'), html.indexOf('<!-- Timeline -->'));
  assert.doesNotMatch(meta, /id="app-version"/);
  const css = read('style.css');
  assert.match(css, /\.app-footer \{[^}]*position: fixed/s);
  assert.match(css, /\.app-footer \{[^}]*bottom: 6px/s);
});

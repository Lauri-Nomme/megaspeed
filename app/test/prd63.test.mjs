/* prd/63 — the v0.10.0 QA fixes' client half.
 *
 * One regression per A/B/C item that is presentation logic, plus source guards
 * for the wiring-only fixes (the request scope, the menu actions, the CSS the
 * page cannot unit-test). The Python server tests cover the API half.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  doneOpenMarkup, orphanedApprovals, overviewCounts, overviewMarkup,
  scopedWaitHours, waitBasis, waitingLabel, whoOwes, chaseCard,
} from '../tables.js';
import { trendChart, trendGeometry, trendStats } from '../trend.js';
import { ruleRow } from '../rules.js';
import { ticketFromQuery } from '../tickets.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

function row(number, ticket, repo, hours, roles, extra = {}) {
  return {
    object_id: `PR:${repo}#${number}`,
    repo, repo_short: repo, number, tickets: [ticket], ticket,
    story_tickets: [], author: extra.author || 'ErLi', state: 'open',
    ball: extra.ball || 'review', ball_label: extra.ball_label || 'review',
    phase: 'review', waiting_since: '2026-10-19T10:43:00Z',
    waiting_hours: hours, age_hours: hours * 2, blocked: false, stale: false,
    stale_reasons: [], waiting_on: Object.keys(roles), waiting_roles: roles,
    reviewers_approved: extra.approved || 0, reviewers_total: Object.keys(roles).length,
    dependents: 0, impact_hours: 0, ...extra,
  };
}

const ROWS = [
  // #53: no approval; CaOs owes the first review — not an orphaned approval
  row(53, 'DEL-167', 'delivery-api', 168, { CaOs: 'review', FrMo: 'sentinel' },
    { stale: true }),
  // #55/#60: peer approved; only the sentinel remains
  row(55, 'DEL-168', 'delivery-api', 152, { FrMo: 'sentinel' },
    { stale: true, approved: 1 }),
  row(60, 'DEL-176', 'delivery-api', 131, { FrMo: 'sentinel' },
    { stale: true, approved: 1 }),
  row(52, 'DEL-197', 'delivery-web', 16, { FrMo: 'sentinel' }, { approved: 1 }),
  // #66: the author owes a fix
  row(66, 'DEL-183', 'delivery-api', 17, { BoFe: 'author', FrMo: 'sentinel' },
    { ball: 'author', ball_label: 'author', author: 'BoFe' }),
];

const DELIVERY = {
  rows: ROWS,
  counts: {
    open: 5, blocked: 0, oldest_waiting: 3, stale: 3, all: 5,
    waiting_week_hours: 40, waiting_week_days: 5,
  },
  done_open: {
    available: true, count: 3, opened_after_close: 1,
    rows: [
      { key: 'DEL-183', repo: 'delivery-api', number: 66, waiting_on: ['BoFe', 'FrMo'] },
      { key: 'DEL-167', repo: 'delivery-api', number: 53, waiting_on: ['CaOs', 'FrMo'] },
      { key: 'DEL-168', repo: 'delivery-api', number: 55, waiting_on: ['FrMo'] },
    ],
  },
};

/* ---------------------------------------------------------------- A1 scope */

test('prd/63 A1: /api/trend accepts the repo scope and the client keeps it', () => {
  const server = read('../server/app.py');
  assert.match(server, /"trend": \("metric", "bucket", "days", "repo"\)/);
  assert.match(server, /court, jira = _repo_scope\(bundle\.court, jira, repo\)/);
  assert.match(server, /"repo": repo,/);
  const main = read('main.js');
  // the repo is part of the trend cache key, not just the request string
  assert.match(main, /const key = `trend:\$\{tenant\}:\$\{state\.timeframe\}:\$\{state\.trendMetric\}:\$\{state\.trendBucket\}:\$\{repo\}:\$\{stamp\}`/);
  assert.match(main, /\/api\/trend\?metric=/);
  assert.match(main, /&\$\{q\}/);
});

/* --------------------------------------------------------------- A2 ticket */

test('prd/63 A2: a bad key is rejected client-side and a 404 is stated', () => {
  assert.equal(ticketFromQuery('del-167'), 'DEL-167');
  assert.equal(ticketFromQuery('  del-167  '), 'DEL-167');
  assert.equal(ticketFromQuery('DEL-9999'), 'DEL-9999');
  assert.equal(ticketFromQuery('foo bar'), '');
  assert.equal(ticketFromQuery('NOPE-1'), 'NOPE-1'); // well shaped → server 404, stated
  const main = read('main.js');
  assert.match(main, /const wanted = ticketFromQuery\(key\)/);
  assert.match(main, /is not a ticket key — a key looks like PROJ-123/);
  assert.match(main, /if \(err\.status === 404\)/);
  assert.match(main, /No ticket \$\{wanted\} in dataset/);
  assert.match(main, /state\.ticketError = null/);
  // a ticket page owns its fetch; load() must not throw on the 404 before
  // __megaspeedReady is set
  assert.match(main, /state\.page !== 'timeline' && state\.page !== 'ticket'/);
});

/* -------------------------------------------------------------- A3 worklog */

test('prd/63 A3: one .worklog-bars owner; the Rules chart has its own class', () => {
  const css = read('style.css');
  assert.equal((css.match(/\.worklog-bars\s*\{/g) || []).length, 1,
    'the duplicate .worklog-bars rule collapsed the ticket bars');
  assert.match(css, /\.worklog-bars \{[^}]*flex-direction: column/s);
  assert.match(css, /\.worklog-chart \{/);
  assert.match(css, /\.worklog-chart \{[^}]*height: 78px/s);
  assert.match(read('worklog.js'), /class="worklog-chart"/);
  assert.doesNotMatch(read('worklog.js'), /class="worklog-bars"/);
  assert.match(read('ticket.js'), /class="worklog-bars"/);
});

/* -------------------------------------------------------------- A4 tenant */

test('prd/63 A4: a generated link keeps the current tenant', () => {
  const main = read('main.js');
  assert.match(main, /function appBase\(\) \{[\s\S]*?const tenant = tenantQuery\(\)/);
  assert.match(main, /return tenant \? `\$\{base\}\?\$\{tenant\}` : base/);
  // every Markdown export and ticket anchor is built from appBase()
  assert.match(main, /ticketMarkdown\(state\.ticket, appBase\(\)\)/);
  assert.match(main, /rulesMarkdown\(state\.rules, appBase\(\)\)/);
  assert.match(main, /entityMarkdown\(entity, hoursMode, appBase\(\), calendarId\)/);
  assert.match(main, /base: appBase\(\)/);
});

/* ---------------------------------------------------------- A5 context menu */

test('prd/63 A5: the menu acts on the right-clicked row and clears in place', () => {
  const main = read('main.js');
  // clear: a scope undo, not a showPage('timeline')
  assert.match(main, /else if \(action === 'clear'\) \{ setScope\('all', 'all'\); \}/);
  // open in timeline: the drawer's own scope + focus path
  assert.match(main, /if \(action === 'timeline'\) \{[\s\S]*?drawerTimelineScope\(entity\)/);
  assert.match(main, /if \(action === 'timeline'\) \{[\s\S]*?timeline\.focusPr\(prId\)/);
});

/* -------------------------------------------------------------- A6 contrast */

test('prd/63 A6: the chase link uses the link token, not the browser blue', () => {
  const css = read('style.css');
  assert.match(css, /\.chase-link \{[^}]*color: var\(--link\)/s);
});

/* --------------------------------------------------------- B7 overview scope */

test('prd/63 B7: Overview tiles and header come from the scoped set', () => {
  const all = overviewCounts(DELIVERY);
  assert.equal(all.open, 5);
  const person = overviewCounts(DELIVERY, { kind: 'person', value: 'CaOs', label: 'CaOs' });
  assert.equal(person.open, 1);
  assert.equal(person.oldest_waiting, 1);
  // measured wait is not the team total under a person/role scope
  const blocking = {
    totals: { hours: 304 },
    edges: [
      { from: 'author', to: 'FrMo', hours: 50 },
      { from: 'review', to: 'FrMo', hours: 70 },
      { from: 'review', to: 'CaOs', hours: 9 },
    ],
  };
  assert.equal(scopedWaitHours(DELIVERY, blocking), 304);
  assert.equal(
    scopedWaitHours(DELIVERY, blocking, { kind: 'person', value: 'CaOs', label: 'CaOs' }),
    9,
  );
  assert.match(waitBasis({ kind: 'person', value: 'CaOs', label: 'CaOs' }), /not response time/);
  // a scoped empty Done-in-Jira list says "in this scope", not "every ticket"
  const empty = doneOpenMarkup({
    done_open: { available: true, count: 3, rows: [] },
  }, { scope: { kind: 'person', value: 'LaAd', label: 'LaAd' } });
  assert.match(empty, /No ticket in this scope is Done in Jira/);
  assert.match(empty, /3 team-wide/);
  assert.doesNotMatch(empty, /Every ticket marked Done in Jira has its code merged/);
});

/* ------------------------------------------------------------- B8 trend latest */

const trend = (overrides = {}) => ({
  metric: 'merged', label: 'Merged PRs', unit: 'count', bucket: 'week',
  available: true,
  points: [
    { start: '2026-11-02T00:00:00Z', end: '2026-11-09T00:00:00Z',
      bucket_start: '2026-11-02T00:00:00Z', label: '2 Nov', in_progress: false,
      available: true, value: 10, n: 10 },
    { start: '2026-11-09T00:00:00Z', end: '2026-11-16T00:00:00Z',
      bucket_start: '2026-11-09T00:00:00Z', label: '9 Nov', in_progress: false,
      available: true, value: 8, n: 8 },
    { start: '2026-11-16T00:00:00Z', end: '2026-11-23T00:00:00Z',
      bucket_start: '2026-11-16T00:00:00Z', label: '16 Nov', in_progress: true,
      available: true, value: 0, n: 0 },
  ],
  summary: {
    first: 10, latest: 8, change: -2, buckets: 3, measured_buckets: 3,
    latest_label: '9 Nov', latest_n: 8, first_label: '2 Nov', first_n: 10,
    latest_provisional: false, settled_buckets: 2, in_progress: ['16 Nov'],
  },
  ...overrides,
});

test('prd/63 B8: the headline is the latest settled bucket, never the in-progress one', () => {
  const stats = trendStats(trend());
  assert.match(stats, /8<\/b> latest \(9 Nov · n=8\)/);
  assert.doesNotMatch(stats, /0<\/b> latest/);
  assert.match(stats, /16 Nov<\/b> in progress \(not in the headline\)/);
  assert.match(stats, /−2 since the first measured bucket/);
  // the count axis has no negative tick
  const geometry = trendGeometry(trend());
  assert.ok(geometry.bounds.low >= 0, `low was ${geometry.bounds.low}`);
  // the in-progress label is inside the plot and not clipped (prd/61 extends
  // the time domain to the last bucket's end, so it is no longer at the edge)
  const chart = trendChart(trend());
  assert.match(chart, />in progress</);
  assert.doesNotMatch(chart, /in progre</);
});

/* ------------------------------------------------------- B9 stale pills + list */

test('prd/63 B9: status pills wrap and the Done-in-Jira section is on Delivery', () => {
  const css = read('style.css');
  assert.match(css, /\.table td\.c-state \.pill \{[^}]*white-space: normal/s);
  assert.match(css, /\.delivery \.c-state \{[^}]*white-space: normal/s);
  const html = read('index.html');
  assert.match(html, /id="delivery-extra"/);
  const main = read('main.js');
  assert.match(main, /doneOpenMarkup\(data, \{\s*base: appBase\(\), scope: deliveryScope\(\), id: 'done-open-delivery'/s);
  // the Overview link names the section it lands on
  assert.match(chaseCard(DELIVERY), /data-anchor="done-open-delivery"/);
});

/* ------------------------------------------------------------ B10 declined PR */

test('prd/63 B10: a declined PR says declined, not a merged-PR multiple', () => {
  // the payload half is pinned in tests/test_benchmark.py; this is the renderer
  const ticket = read('ticket.js');
  assert.match(ticket, /pr\.cycle_benchmark && pr\.cycle_benchmark\.declined/);
  assert.match(ticket, />declined</);
});

/* ---------------------------------------------------------------- C11 metric */

test('prd/63 C11: each constraint surface names its clock', () => {
  assert.match(waitBasis(null), /queue-wait hours.*not response time/);
  const tables = read('tables.js');
  assert.match(tables, /queue-wait hours by handover/);
  assert.match(tables, /queue-wait, not response time/);
  const main = read('main.js');
  assert.match(main, /queue-wait hours/);
  assert.match(main, /response time is the table below/);
});

/* -------------------------------------------------------------- C12 holder */

test('prd/63 C12: the orphan list names the real holder(s) per PR', () => {
  const orphans = orphanedApprovals(DELIVERY);
  assert.deepEqual(orphans.map((entry) => entry.number), [55, 60, 52]);
  // #53 has no approval yet and #66 is the author's — neither is sentinel-only
  assert.ok(!orphans.some((entry) => entry.number === 53 || entry.number === 66));
  const html = chaseCard(DELIVERY);
  // every row names its holder set and opens the PR drawer
  assert.match(html, /#55[\s\S]*?FrMo \(sentinel\)/);
  assert.match(html, /data-chase-open="PR:delivery-api#55"/);
});

/* ------------------------------------------------------- C13/C14 rules facts */

test('prd/63 C14: a lab-contradicted rule carries a visible caveat', () => {
  const html = ruleRow({
    id: 'flow-efficiency', rank: 9, severity: 'warning', metric: 'flow_efficiency',
    title: 'Flow efficiency is 18%', suggestion: 'Adopt a first-response SLA',
    impact_hours: 10, confidence: 0.5,
    evidence: { prs: 10 },
    policy_lab: {
      contradiction: true, note: 'the policy lab models this at +99 % cycle — not a measured win',
    },
  });
  assert.match(html, /data-lab-contradiction="true"/);
  assert.match(html, /not a proven fix/);
  const clean = ruleRow({
    id: 'round-cap', rank: 1, severity: 'warning', metric: 'rework_loops',
    title: 'Cap rounds', suggestion: 'Cap at 2', impact_hours: 10,
    confidence: 0.5, evidence: { prs: 10 },
    policy_lab: { contradiction: false, supports: true, note: '−13 % cycle' },
  });
  assert.doesNotMatch(clean, /data-lab-contradiction/);
});

/* ------------------------------------------------------------ D papercuts */

test('prd/63 D: Delivery "waiting on" and Overview "who owes" agree', () => {
  const authorOwes = ROWS[4];
  assert.equal(waitingLabel(authorOwes), whoOwes(authorOwes));
  assert.equal(waitingLabel(authorOwes), 'BoFe (author) + FrMo (sentinel)');
});

test('prd/63 D: the Delivery median-× reason and the size-rule reason are stated', () => {
  const main = read('main.js');
  assert.match(main, /median × unavailable/);
  assert.match(main, /function prSizeWhy\(\)/);
  assert.match(main, /a large-vs-small comparison needs at least 2 of each/);
  assert.match(read('prsize.js'), /data-pr-size-why/);
});

test('prd/63 D: the build tag is in flow and the phone columns wrap', () => {
  const html = read('index.html');
  const nav = html.slice(html.indexOf('<div class="nav-foot"'), html.indexOf('</nav>'));
  assert.match(nav, /id="app-version"/);
  assert.doesNotMatch(html, /class="app-footer"/);
  const css = read('style.css');
  assert.doesNotMatch(css, /\.app-footer/);
  assert.match(css, /\.ticket-head \.pill \{[^}]*white-space: normal/s);
  assert.match(css, /\.sprint-row \{[^}]*max-width: 100%/s);
  // the overview card sits above the tiles and counts the same scoped rows
  const htmlOverview = overviewMarkup({
    delivery: DELIVERY, queues: { level: 'person', streams: [] }, blocking: null,
  });
  assert.ok(htmlOverview.indexOf('chase-panel') < htmlOverview.indexOf('class="stats"'));
});

/* prd/54 — "what to do now": chase the orphaned approvals, or adopt a review SLA.
 *
 * The card is a thin presentation over the delivery list's holder set. These
 * tests pin the selection rule (sentinel-only, or stale with the sentinel still
 * owing — never a normal peer review whose gate has not opened), the names and
 * links for #55/#60/#52/#53, and the lab's review-SLA affordance.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  chaseCard, orphanedApprovals, sentinelNames, waitsOnSentinel, whoOwes,
} from '../tables.js';
import { overviewMarkup } from '../tables.js';

function row(number, ticket, repo, hours, roles, { stale = false, ...extra } = {}) {
  return {
    object_id: `PR:${repo}#${number}`,
    id: `${repo}#${number}`,
    repo,
    repo_short: repo,
    number,
    tickets: [ticket],
    ticket,
    story_tickets: [],
    author: 'ErLi',
    state: 'open',
    ball: 'review',
    ball_label: 'review',
    phase: 'review',
    waiting_since: '2026-10-19T10:43:00Z',
    waiting_hours: hours,
    age_hours: hours * 2,
    blocked: false,
    stale,
    stale_reasons: stale ? ['open 24 business days (limit 5)'] : [],
    waiting_on: Object.keys(roles),
    waiting_roles: roles,
    dependents: 0,
    impact_hours: 0,
    ...extra,
  };
}

// The QA store's eight open PRs, in the shape the delivery API returns them.
const ROWS = [
  row(53, 'DEL-167', 'delivery-api', 168, { CaOs: 'review', FrMo: 'sentinel' }, { stale: true }),
  row(55, 'DEL-168', 'delivery-api', 152, { FrMo: 'sentinel' }, { stale: true }),
  row(60, 'DEL-176', 'delivery-api', 131, { FrMo: 'sentinel' }, { stale: true }),
  row(52, 'DEL-197', 'delivery-web', 16, { FrMo: 'sentinel' }),
  // a normal two-day review with a peer still pending: the sentinel's gate has
  // not opened (concepts.md §2), so it is not an orphaned approval
  row(51, 'DEL-190', 'delivery-api', 16, { CaOs: 'review', FrMo: 'sentinel' }),
  // waits on its author, not the sentinel
  row(66, 'DEL-183', 'delivery-api', 40, { ErLi: 'author' }),
];

const DELIVERY = {
  rows: ROWS,
  counts: {
    open: ROWS.length, blocked: 0, oldest_waiting: 3, stale: 3, all: ROWS.length,
    waiting_week_hours: 40, waiting_week_days: 5,
  },
  done_open: {
    available: true,
    count: 3,
    opened_after_close: 1,
    rows: [
      { key: 'DEL-183', repo: 'delivery-api', number: 66, waiting_on: ['CaOs'] },
      { key: 'DEL-167', repo: 'delivery-api', number: 53, waiting_on: ['CaOs', 'FrMo'] },
      { key: 'DEL-168', repo: 'delivery-api', number: 55, waiting_on: ['FrMo'] },
    ],
  },
};

test('the orphans are the sentinel-only waits plus the stale shared waits', () => {
  const orphans = orphanedApprovals(DELIVERY);
  // worst wait first: #53, #55, #60, then the fresh #52
  assert.deepEqual(orphans.map((entry) => entry.number), [53, 55, 60, 52]);
  // excluded: #51 (peer still pending, not stale) and #66 (waits on the author)
  assert.deepEqual(orphans.map((entry) => entry.ticket).sort(),
    ['DEL-167', 'DEL-168', 'DEL-176', 'DEL-197']);
  assert.equal(waitsOnSentinel(ROWS[4]), true);   // #51 does wait on the sentinel…
  assert.equal(waitsOnSentinel(ROWS[5]), false);  // …#66 does not
  assert.deepEqual(sentinelNames(orphans), ['FrMo']);
});

test('who owes each names the role, not only the pseudonym', () => {
  assert.equal(whoOwes(ROWS[0]), 'CaOs (review) + FrMo (sentinel)');
  assert.equal(whoOwes(ROWS[1]), 'FrMo (sentinel)');
  assert.equal(whoOwes(null), '—');
});

test('the card renders the four orphans, who owes, and a chase link each', () => {
  const html = chaseCard(DELIVERY);
  assert.match(html, /id="chase"/);
  assert.match(html, /What to do now/);
  assert.match(html, /data-chase-count="4"/);
  for (const ticket of ['DEL-167', 'DEL-168', 'DEL-176', 'DEL-197']) {
    assert.match(html, new RegExp(`data-chase-ticket="${ticket}"`));
    assert.match(html, new RegExp(`href="#ticket/${ticket}"`));
    assert.match(html, new RegExp(`data-ticket="${ticket}"`));
  }
  // #55 and #60 name the sentinel alone; #53 names both holders
  assert.match(html, /#55[\s\S]*?FrMo \(sentinel\)/);
  assert.match(html, /#60[\s\S]*?FrMo \(sentinel\)/);
  assert.match(html, /#53[\s\S]*?CaOs \(review\) \+ FrMo \(sentinel\)/);
  assert.equal((html.match(/class="chip chase-link"/g) || []).length, 4);
  assert.match(html, /chase →/);
  // the review SLA is the lab's own preset, one click from here
  assert.match(html, /data-preset="sla4"/);
  assert.match(html, /data-go="policy"/);
  assert.match(html, /review SLA 4 h scenario/);
  // the done-but-open signal travels too
  assert.match(html, /3 tickets are Done in Jira with the PR open/);
  assert.match(html, /data-go="delivery"/);
});

test('a clean delivery list renders no card; done-but-open alone keeps it', () => {
  const clean = {
    rows: [ROWS[4], ROWS[5]],
    counts: { open: 2, blocked: 0, oldest_waiting: 0, stale: 0, all: 2 },
    done_open: { count: 0, rows: [] },
  };
  assert.equal(chaseCard(clean), '');
  const doneOnly = {
    rows: [ROWS[5]],
    counts: { open: 1, blocked: 0, oldest_waiting: 0, stale: 0, all: 1 },
    done_open: { count: 2, rows: [] },
  };
  const html = chaseCard(doneOnly);
  assert.match(html, /no orphaned approval/);
  assert.match(html, /data-go="delivery"/);
  assert.doesNotMatch(html, /data-chase-ticket/);
});

test('the card follows the scope like the attention list', () => {
  const repoCard = chaseCard(DELIVERY, { scope: { kind: 'repo', value: 'delivery-dev' } });
  assert.equal(repoCard, '');
  const webCard = chaseCard(DELIVERY, { scope: { kind: 'repo', value: 'delivery-web' } });
  assert.match(webCard, /data-chase-count="1"/);
  assert.match(webCard, /data-chase-ticket="DEL-197"/);
  const personCard = chaseCard(DELIVERY, { scope: { kind: 'person', value: 'CaOs' } });
  assert.match(personCard, /data-chase-count="1"/);
  assert.match(personCard, /data-chase-ticket="DEL-167"/);
});

test('the Overview puts the card above the tiles', () => {
  const html = overviewMarkup({
    delivery: DELIVERY,
    queues: { level: 'person', streams: [], legend: { people: [] } },
    blocking: null,
  });
  assert.ok(html.indexOf('chase-panel') > -1);
  assert.ok(html.indexOf('chase-panel') < html.indexOf('class="stats"'));
  assert.match(html, /4 orphaned approvals/);
});

/* prd/64 G3 — scope correctness (F-07, F-08).

 * F-07: a person scope is "waiting on X" and X must be a holder. With several
 * holders the row keeps the role label; `waiting_on`/`waiting_roles` name the
 * real holders, and `ball_label` is never a person. PR #66 waits on BoFe + FrMo
 * — the CaOs whose review just ended must not put it in `person:CaOs`.
 * F-08: "Done in Jira, PR open" filters with the same `matchesScope`, so
 * `role:sentinel` lists the rows whose `waiting_roles` say so instead of
 * comparing "sentinel" against a pseudonym.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { doneOpenMarkup, matchesScope } from '../tables.js';

const row = (overrides) => ({
  object_id: `PR:delivery-api#${overrides.number}`,
  repo: 'ENG/delivery-api',
  repo_short: 'delivery-api',
  state: 'open',
  tickets: [overrides.key],
  story_tickets: [],
  blocked: false,
  ...overrides,
});

// #66: CaOs's blocker comment (13:48) handed the ball back to the author while
// FrMo still owes a verdict — two holders, so the row keeps the role label.
const PR_66 = row({
  number: 66, key: 'DEL-183', author: 'BoFe', ball: 'author', ball_label: 'author',
  waiting_for: 'author', waiting_on: ['BoFe', 'FrMo'],
  waiting_roles: { BoFe: 'author', FrMo: 'sentinel' },
  waiting_hours: 17,
});
// #53: CaOs owes the first review (the row CaOs actually holds).
const PR_53 = row({
  number: 53, key: 'DEL-167', author: 'ErLi', ball: 'review', ball_label: 'review',
  waiting_for: 'review', waiting_on: ['CaOs', 'FrMo'],
  waiting_roles: { CaOs: 'review', FrMo: 'sentinel' },
  waiting_hours: 168,
});
// #55: only the sentinel remains.
const PR_55 = row({
  number: 55, key: 'DEL-168', author: 'ErLi', ball: 'review', ball_label: 'FrMo',
  waiting_for: 'FrMo', waiting_on: ['FrMo'],
  waiting_roles: { FrMo: 'sentinel' },
  waiting_hours: 152,
});

const DELIVERY = {
  rows: [PR_66, PR_53, PR_55],
  done_open: {
    available: true,
    count: 3,
    opened_after_close: 1,
    rows: [
      { key: 'DEL-183', object_id: 'PR:delivery-api#66', repo: 'ENG/delivery-api', number: 66, waiting_on: ['BoFe', 'FrMo'] },
      { key: 'DEL-167', object_id: 'PR:delivery-api#53', repo: 'ENG/delivery-api', number: 53, waiting_on: ['CaOs', 'FrMo'] },
      { key: 'DEL-168', object_id: 'PR:delivery-api#55', repo: 'ENG/delivery-api', number: 55, waiting_on: ['FrMo'] },
    ],
  },
};

const scope = (kind, value) => ({ kind, value, label: value });
const doneRows = (html) => [...html.matchAll(/data-done-open="([^"]+)"/g)].map((m) => m[1]);

/* ------------------------------------------------------------------- F-07 */

test('prd/64 F-07: person:CaOs excludes #66 (it waits on BoFe + FrMo)', () => {
  const caos = scope('person', 'CaOs');
  assert.equal(matchesScope(PR_66, caos), false);
  assert.equal(matchesScope(PR_53, caos), true);
  assert.equal(matchesScope(PR_55, caos), false);
  // the holders, not the author, are the scope: BoFe authors #66 and still
  // owes a fix, so BoFe holds it — but nobody else's review puts them there.
  assert.equal(matchesScope(PR_66, scope('person', 'BoFe')), true);
  assert.equal(matchesScope(PR_66, scope('person', 'FrMo')), true);
});

test('prd/64 F-07: an old payload that leaked waiting_for cannot widen a scope', () => {
  // `waiting_on` is the truth whenever it is there: the leaked name on an
  // older API row does not become a holder.
  const leaked = { ...PR_66, waiting_for: 'CaOs' };
  assert.equal(matchesScope(leaked, scope('person', 'CaOs')), false);
  assert.equal(matchesScope(leaked, scope('person', 'FrMo')), true);
});

test('prd/64 F-07: ball_label is a label, never a person scope', () => {
  // a row whose only holder is FrMo but whose label came from an ended wait
  const leaky = { ...PR_55, ball_label: 'CaOs' };
  assert.equal(matchesScope(leaky, scope('person', 'CaOs')), false);
  // an older payload without waiting_on still answers from waiting_for
  const old = { ball: 'review', waiting_for: 'CaOs' };
  assert.equal(matchesScope(old, scope('person', 'CaOs')), true);
});

test('prd/64 F-07: role scopes read waiting_roles, not the pseudonyms', () => {
  assert.equal(matchesScope(PR_66, scope('role', 'sentinel')), true);
  assert.equal(matchesScope(PR_66, scope('role', 'author')), true);
  assert.equal(matchesScope(PR_66, scope('role', 'review')), false);
  assert.equal(matchesScope(PR_53, scope('role', 'review')), true);
  assert.equal(matchesScope(PR_53, scope('role', 'sentinel')), true);
  assert.equal(matchesScope(PR_55, scope('role', 'sentinel')), true);
  assert.equal(matchesScope(PR_55, scope('role', 'author')), false);
  // a pseudonym is not a role
  assert.equal(matchesScope(PR_55, scope('role', 'FrMo')), false);
});

/* ------------------------------------------------------------------- F-08 */

test('prd/64 F-08: role:sentinel lists all 3 Done-open PRs', () => {
  const html = doneOpenMarkup(DELIVERY, { scope: scope('role', 'sentinel') });
  assert.deepEqual(doneRows(html), ['DEL-183', 'DEL-167', 'DEL-168']);
  assert.doesNotMatch(html, /No ticket in this scope is Done in Jira/);
  // the pill counts the rows this view shows
  assert.match(html, /<span class="pill blocked">3<\/span>/);
});

test('prd/64 F-08: role:author and role:review list their own rows', () => {
  const author = doneOpenMarkup(DELIVERY, { scope: scope('role', 'author') });
  assert.deepEqual(doneRows(author), ['DEL-183']);
  const review = doneOpenMarkup(DELIVERY, { scope: scope('role', 'review') });
  assert.deepEqual(doneRows(review), ['DEL-167']);
  // a role nobody holds in a specific way says "none in this scope"
  const none = doneOpenMarkup(DELIVERY, { scope: scope('role', 'ready') });
  assert.deepEqual(doneRows(none), []);
  assert.match(none, /No ticket in this scope is Done in Jira with its PR open — 3 team-wide/);
});

test('prd/64 F-08: person, repo, ticket and all keep their old answers', () => {
  assert.deepEqual(
    doneRows(doneOpenMarkup(DELIVERY, { scope: scope('person', 'FrMo') })),
    ['DEL-183', 'DEL-167', 'DEL-168'],
  );
  assert.deepEqual(
    doneRows(doneOpenMarkup(DELIVERY, { scope: scope('person', 'CaOs') })),
    ['DEL-167'],
  );
  assert.deepEqual(
    doneRows(doneOpenMarkup(DELIVERY, { scope: scope('person', 'Nobody') })),
    [],
  );
  assert.deepEqual(
    doneRows(doneOpenMarkup(DELIVERY, { scope: scope('repo', 'delivery-web') })),
    [],
  );
  assert.deepEqual(
    doneRows(doneOpenMarkup(DELIVERY, { scope: scope('ticket', 'DEL-183') })),
    ['DEL-183'],
  );
  // no scope is unchanged: every row, store-wide count
  assert.deepEqual(doneRows(doneOpenMarkup(DELIVERY)), ['DEL-183', 'DEL-167', 'DEL-168']);
  assert.match(doneOpenMarkup(DELIVERY), /<span class="pill blocked">3<\/span>/);
});

test('prd/64 F-08: a ticket scope still answers when the court row is absent', () => {
  const bare = {
    done_open: {
      available: true, count: 1,
      rows: [{
        key: 'DEL-183', repo: 'ENG/delivery-api', repo_short: 'delivery-api',
        number: 66, waiting_on: ['BoFe', 'FrMo'],
      }],
    },
  };
  assert.deepEqual(
    doneRows(doneOpenMarkup(bare, { scope: scope('ticket', 'DEL-183') })),
    ['DEL-183'],
  );
  assert.deepEqual(
    doneRows(doneOpenMarkup(bare, { scope: scope('repo', 'delivery-api') })),
    ['DEL-183'],
  );
});

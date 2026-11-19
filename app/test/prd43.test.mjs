/* prd/43 — reconcile the Rules and Policy-lab advice, and close the last gaps.
 *
 * One regression per item, at the layer where the bug lived:
 *
 *   1. a contradicted rule carries the lab's note; the lab's best is a line;
 *   2. the sentinel's response clock is gate-aware in the Queue table;
 *   3. the ticket-page drawer's Timeline chip scopes to the ticket;
 *   4. the "longest mean wait" tile counts people, not the synthetic stream;
 *   5. one PR, one set of numbers (the UI reads the server's canonical facts).
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { labBestLine, labNote, rulesMarkdown, rulesMarkup } from '../rules.js';
import { drawerTimelineScope } from '../scope.js';
import { overviewMarkup } from '../tables.js';
import { responseTable } from '../queues.js';
import { sprintStrip } from '../ticket.js';
import { sprintOutcomeLine } from '../sprints.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const CONTRADICTED = {
  id: 'jira-waiting-time', rank: 1, severity: 'warning',
  metric: 'waiting_time_decomposition',
  title: 'The team spends 29% of ticket cycle time in the created → ready segment',
  suggestion: 'Refine before the sprint.', impact_hours: 1200, confidence: 1,
  evidence: {
    segment: 'created → ready', segment_hours: 2400, share: 0.29,
    impact_basis: 'assumption: half the largest segment\'s hours — not a measured saving',
    impact_assumed: true,
  },
  scope: { kind: 'team' },
  policy_lab: {
    policy: 'refine', label: 'refine before start', cycle_percent: 82,
    contradiction: true, supports: false,
    note: 'the policy lab models this at +82 % cycle — not a measured win',
  },
};

const REPORT = {
  available: true,
  best: { id: 'roundcap2', label: 'cap review at 2 rounds', cycle_percent: -13 },
  note: "the lab's best scenario is cap review at 2 rounds — −13 % cycle",
  policies: [],
};

/* ---- 1. Rules ↔ Policy-lab cross-check -------------------------------- */

test('a contradicted rule carries the lab note and stays an assumption', () => {
  const note = labNote(CONTRADICTED);
  assert.match(note, /class="rule-lab contradiction"/);
  assert.match(note, /\+82 % cycle — not a measured win/);
  const data = {
    level: 'team', sample: { prs: 3, days: 30 }, hours_mode: 'business',
    as_of: '2026-11-17T00:00:00Z', note: '', metrics: {}, policy_lab: REPORT,
    suggestions: [CONTRADICTED],
  };
  const html = rulesMarkup(data);
  // the at-stake figure is visibly an assumption, with its own footnote
  assert.match(html, /at stake <span class="muted">\(assumed\)<\/span>/);
  assert.match(html, /at stake is an assumption .*not a measured saving/);
  // the lab's verdict on the rule and its best scenario, on the page
  assert.match(html, /the policy lab models this at \+82 % cycle — not a measured win/);
  assert.match(html, /data-lab-best/);
  assert.match(html, /the lab's best scenario is cap review at 2 rounds/);
  const markdown = rulesMarkdown(data);
  assert.match(markdown, /- policy lab: the policy lab models this at \+82 % cycle/);
  assert.match(markdown, /the lab's best scenario is cap review at 2 rounds/);
});

test('a supported rule says what the lab models, and the best line is pure', () => {
  const supported = {
    ...CONTRADICTED, id: 'round-cap',
    policy_lab: {
      policy: 'roundcap2', label: 'cap review at 2 rounds', cycle_percent: -13,
      contradiction: false, supports: true,
      note: 'the policy lab models this at −13 % cycle',
    },
    evidence: {},
  };
  assert.match(labNote(supported), /rule-lab supports/);
  assert.match(labNote(supported), /−13 % cycle/);
  assert.equal(labBestLine({ policy_lab: REPORT }), REPORT.note);
  assert.equal(labBestLine({ policy_lab: { available: false } }), '');
});

/* ---- 3. the ticket-page drawer's Timeline chip ------------------------- */

test('the drawer Timeline chip scopes to the ticket from every opener', () => {
  // ticket-page drawer: the key is carried at the entity level (prd/43 §3)
  assert.deepEqual(
    drawerTimelineScope({ ticket: 'DEL-167', pr: { id: 'PR:web#53' } }),
    { kind: 'ticket', value: 'DEL-167', label: 'DEL-167' },
  );
  // delivery/timeline drawer: the PR carries its own key
  assert.deepEqual(
    drawerTimelineScope({
      pr: { id: 'PR:web#53', ticket: 'DEL-167', actor: 'CaOs' },
    }),
    { kind: 'ticket', value: 'DEL-167', label: 'DEL-167' },
  );
  // a row-only entity still finds the ticket, never the author
  assert.deepEqual(
    drawerTimelineScope({ row: { object_id: 'PR:web#52', tickets: ['DEL-1', 'DEL-167'] } }),
    { kind: 'ticket', value: 'DEL-167', label: 'DEL-167' },
  );
  // no ticket at all: the PR's author is the honest fallback, not "everything"
  assert.deepEqual(
    drawerTimelineScope({ pr: { id: 'PR:web#9', actor: 'AaAa' } }),
    { kind: 'person', value: 'AaAa', label: 'AaAa' },
  );
  assert.equal(drawerTimelineScope(null), null);
  // the page wires the helper (and the ticket drawer carries the key)
  const main = read('main.js');
  assert.match(main, /drawerTimelineScope\(entity\)/);
  assert.match(main, /ticket: key,\n    pr: \{/);
});

/* ---- 4. the overview tile counts people only --------------------------- */

function stream(id, hours, diagnosis, role = 'person') {
  return {
    id,
    role,
    summary: { mean_wait_hours: hours, diagnosis },
  };
}

test('the longest mean wait tile reads a person, not the review stream', () => {
  const queues = {
    level: 'person',
    // the synthetic role stream is listed first by the API
    streams: [
      stream('review', 22.6, 'slow', 'synthetic'),
      stream('FrMo', 11.1, 'overloaded'),
      stream('ErLi', 9.9, 'slow'),
    ],
  };
  const delivery = {
    counts: { open: 0, blocked: 0, oldest_waiting: 0, stale: 0 },
    rows: [],
  };
  const html = overviewMarkup({ delivery, queues });
  // 11.1 h = 1 d 3 h at 8 business hours a day; the review stream's 22.6 h is
  // not a person and must not be the headline.
  assert.match(html, /longest mean wait ≈1 d 3 h/);
  assert.doesNotMatch(html, /longest mean wait ≈2 d 6 h/);
  // and the counts are people too
  assert.match(html, /<span class="stat-n">1<\/span>\s*<span class="stat-k">overloaded person/);
});

/* ---- 2. Frank's response clock is gate-aware --------------------------- */

test('the response table says the sentinel clock starts at the last peer', () => {
  const data = {
    response: {
      reviewer_median_hours: 2.0,
      sentinel_after_peers: true,
      people: [
        {
          person: 'FrMo', role: 'sentinel', median_hours: 6.0, mean_hours: 6.4,
          p90_hours: 12.9, never: 2, never_oldest_hours: 152,
          sentinel_lag_n: 8, sentinel_lag_median_hours: 6.0,
        },
        {
          person: 'AlNg', role: 'reviewer', median_hours: 2.0, mean_hours: 2.2,
          p90_hours: 4.0, never: 0,
        },
      ],
    },
  };
  const html = responseTable(data);
  assert.match(html, /sentinel: last peer answer first/);
  assert.match(html, /last peer answer → first response/);
  assert.match(html, /×3\.0/);                    // 6 h vs a 2 h peer median
  assert.match(html, /oldest open request 19 d 0 h/);   // the forgotten tail
  assert.match(html, /\(19 d 0 h\)/);              // and it is on screen, not only in a title
});

/* ---- 5. one PR, one set of numbers / sprint delivery ------------------- */

test('a sprint chip marks a Jira-done story with an unmerged PR', () => {
  const html = sprintStrip({
    sprints: {
      memberships: [{
        sprint_id: 3, start: '2026-11-09T00:00:00Z', end: null,
        planned_end: '2026-11-27', state: 'active', added: '2026-11-09T00:00:00Z',
        outcome: 'unmerged',
      }],
      carried_over: false, late: 0, never: 0, unmerged: 1,
    },
  });
  assert.match(html, /done in Jira · PR unmerged/);
  assert.doesNotMatch(html, /done by end/);
  // the sprint lens says the same thing in words
  assert.equal(
    sprintOutcomeLine({
      story_count: 30, done_by_end: 25, done_late: 0, never_done: 2, unmerged: 3,
    }),
    '25 of 30 done by end · 3 Done in Jira · PR unmerged · 2 still open',
  );
});

test('the drawers read the canonical commit count and business-hour phases', () => {
  const main = read('main.js');
  // commits: the delivery row's canonical count, then the PR's own
  assert.match(main, /row && row\.commits != null/);
  // phase spans: the server's business-hour map when the row carries it
  assert.match(main, /row\.phase_hours/);
  assert.match(main, /cycle, wait and phase spans are business hours/);
});

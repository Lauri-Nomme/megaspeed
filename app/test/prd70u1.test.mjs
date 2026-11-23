/* prd/70 U1 — one canonical ticket (F-03) and one holder story (F-09).
 *
 * The client used to take `keys[keys.length - 1]`: `#11030` (EPIC + STORY)
 * showed `DEMO-57507` but `#11096` (open STORY + 2023-done STORY) showed the
 * 2023 ticket, while the Aging table showed the first key of each. The store's
 * own object rows now drive one pick (`primaryTicket`), mirroring the server's
 * `analysis.ballcourt.primary_ticket`, and the holder vocabulary is the
 * server's (`holder_label`: `review queue` / `ready to merge`).
 *
 * Run with: node --test app/test/*.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  annotateRow, epicTicket, indexTickets, keyListsOf, primaryTicket, rowTicket,
  ticketFields,
} from '../tickets.js';
import { holderWords, waitingLabel, whoOwes } from '../tables.js';
import { holderLabel } from '../aging.js';

/* The combined-corpus shape: an epic next to its story, and a PR that names a
 * 2023 story next to the one opened today. */
const OBJECTS = [
  { object_type: 'EPIC', object_id: 'EPIC:DEMO-56715', ticket_keys: ['DEMO-56715'], state: 'open' },
  { object_type: 'STORY', object_id: 'STORY:DEMO-57507', ticket_keys: ['DEMO-57507'], state: 'done' },
  { object_type: 'STORY', object_id: 'STORY:DEMO-60130', ticket_keys: ['DEMO-60130'], state: 'open' },
  { object_type: 'STORY', object_id: 'STORY:DEMO-9714', ticket_keys: ['DEMO-9714'], state: 'done' },
  { object_type: 'STORY', object_id: 'STORY:DEMO-60089', ticket_keys: ['DEMO-60089'], state: 'open' },
  { object_type: 'PR', object_id: 'PR:DEMO/widget#11030', ticket_keys: ['DEMO-56715', 'DEMO-57507'] },
  { object_type: 'PR', object_id: 'PR:DEMO/widget#11096', ticket_keys: ['DEMO-60130', 'DEMO-9714'] },
  { object_type: 'PR', object_id: 'PR:DEMO/widget#10865', ticket_keys: ['DEMO-56715'] },
];

const KINDS = {
  'DEMO-56715': 'EPIC',
  'DEMO-57507': 'STORY',
  'DEMO-60130': 'STORY',
  'DEMO-9714': 'STORY',
};
const STATES = {
  'DEMO-56715': 'open',
  'DEMO-57507': 'done',
  'DEMO-60130': 'open',
  'DEMO-9714': 'done',
};

test('F-03: a STORY key beats the EPIC key next to it', () => {
  assert.equal(
    primaryTicket(['DEMO-56715', 'DEMO-57507'], { kindByKey: KINDS, stateByKey: STATES }),
    'DEMO-57507',
  );
});

test('F-03: the open story beats the one closed in 2023', () => {
  assert.equal(
    primaryTicket(['DEMO-60130', 'DEMO-9714'], { kindByKey: KINDS, stateByKey: STATES }),
    'DEMO-60130',
  );
});

test('F-03: a tie between live stories picks the newer number', () => {
  assert.equal(
    primaryTicket(['DEMO-40000', 'DEMO-60130'], {
      kindByKey: { 'DEMO-40000': 'STORY', 'DEMO-60130': 'STORY' },
      stateByKey: { 'DEMO-40000': 'open', 'DEMO-60130': 'open' },
    }),
    'DEMO-60130',
  );
});

test('F-03: no keys is empty, an unknown key is still usable', () => {
  assert.equal(primaryTicket([], {}), '');
  assert.equal(primaryTicket([null, '  ']), '');
  assert.equal(primaryTicket(['DEMO-99999']), 'DEMO-99999');
});

test('F-03: the object census labels a multi-key PR with its canonical story', () => {
  keyListsOf(OBJECTS);
  const epicAndStory = annotateRow({ tickets: ['DEMO-56715', 'DEMO-57507'] });
  assert.equal(epicAndStory.ticket, 'DEMO-57507');
  assert.equal(epicAndStory.epic, 'DEMO-56715');
  const twoStories = annotateRow({ tickets: ['DEMO-60130', 'DEMO-9714'] });
  assert.equal(twoStories.ticket, 'DEMO-60130');
  // a PR that names only the epic keeps it (the epic page lists it)
  assert.equal(ticketFields(['DEMO-56715']).ticket, 'DEMO-56715');
});

test('F-03: a TICKET row cannot downgrade an EPIC (alpha object order)', () => {
  // `/api/objects` orders by type: EPIC before TICKET. A TICKET row for the
  // same key used to overwrite the EPIC kind, putting the epic in the story
  // tier where its `open` state beat the done story it sits next to.
  indexTickets([
    { object_type: 'EPIC', object_id: 'EPIC:DEMO-56715', ticket_keys: ['DEMO-56715'], state: 'open' },
    { object_type: 'TICKET', object_id: 'TICKET:DEMO-56715', ticket_keys: ['DEMO-56715'], state: 'open' },
    { object_type: 'STORY', object_id: 'STORY:DEMO-57507', ticket_keys: ['DEMO-57507'], state: 'done' },
  ]);
  assert.equal(rowTicket({ tickets: ['DEMO-56715', 'DEMO-57507'] }), 'DEMO-57507');
  indexTickets(OBJECTS);   // restore the shared index for the other tests
});

test('F-03: a row that was never annotated reads the same canonical key', () => {
  indexTickets(OBJECTS);
  assert.equal(rowTicket({ tickets: ['DEMO-56715', 'DEMO-57507'] }), 'DEMO-57507');
  assert.equal(rowTicket({ tickets: ['DEMO-60130', 'DEMO-9714'] }), 'DEMO-60130');
});

test('F-03: keyListsOf still returns only PR key lists', () => {
  const lists = keyListsOf(OBJECTS);
  assert.deepEqual(lists, [
    ['DEMO-56715', 'DEMO-57507'],
    ['DEMO-60130', 'DEMO-9714'],
    ['DEMO-56715'],
  ]);
});

test('F-03: without a store index the census rule still answers', () => {
  indexTickets([]);   // no STORY/EPIC rows: the index is cleared
  const row = annotateRow({ tickets: ['DEL-1', 'DEL-167'] }, new Set(['DEL-1']));
  assert.equal(row.ticket, 'DEL-167');
});

test('F-09: the queue labels are the server’s words, not "nobody"', () => {
  // the strings the server writes (analysis.ballcourt.REVIEW_QUEUE / READY_TO_MERGE)
  assert.equal(holderWords({ holder_label: 'review queue' }), 'review queue');
  assert.equal(holderWords({ holder_label: 'ready to merge' }), 'ready to merge');
  // an older payload carries only the ball label
  assert.equal(holderWords({ ball: 'review' }), 'review queue');
  assert.equal(holderWords({ ball: 'ready' }), 'ready to merge');
  assert.equal(holderWords(null), '—');
});

test('F-09: Delivery’s "waiting on" names the live queue for an unassigned PR', () => {
  const unassigned = { waiting_on: [], ball: 'review', holder_label: 'review queue' };
  assert.equal(waitingLabel(unassigned), 'review queue');
  assert.equal(whoOwes(unassigned), 'review queue');
  const ready = { waiting_on: [], ball: 'ready', holder_label: 'ready to merge' };
  assert.equal(waitingLabel(ready), 'ready to merge');
  // named holders still win (concepts.md §2: a single holder is named)
  assert.equal(
    waitingLabel({ waiting_on: ['FrMo'], waiting_roles: { FrMo: 'sentinel' }, ball: 'review' }),
    'FrMo (sentinel)',
  );
});

test('F-09: the three surfaces render the same holder for the same PR', () => {
  // Aging: the server row's holder cell (app/aging.js) over the payload entry
  const agingRow = { holder: [{ person: 'review queue', role: '', hours: 40 }] };
  assert.equal(holderLabel(agingRow), 'review queue');
  // Delivery: the same attribution through holder_label
  assert.equal(waitingLabel({ waiting_on: [], holder_label: 'review queue' }), 'review queue');
  // Ticket detail: the payload's open PR names the queue in waiting_on
  assert.equal(waitingLabel({ waiting_on: ['review queue'], ball: 'review' }), 'review queue');
  // and a ready PR agrees on the merge words everywhere
  assert.equal(holderLabel({ holder: [{ person: 'ready to merge', role: '' }] }), 'ready to merge');
  assert.equal(waitingLabel({ waiting_on: [], holder_label: 'ready to merge' }), 'ready to merge');
  assert.equal(waitingLabel({ waiting_on: ['ready to merge'] }), 'ready to merge');
});

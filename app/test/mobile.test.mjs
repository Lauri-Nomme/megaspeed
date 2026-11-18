/* prd/28 — the phone shell's decisions and the card markup, without a browser.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  PHONE_MAX, isPhoneWidth, phoneEntryPage, phoneEntryLanes, hintText,
  shouldDismissSheet, sheetOffset, touchAxis,
} from '../mobile.js';
import { deliveryTable } from '../tables.js';
import { prTable, eventsList } from '../ticket.js';
import { rulesMarkup } from '../rules.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

test('the phone breakpoint is <= 640px and nothing wider', () => {
  assert.equal(PHONE_MAX, 640);
  assert.equal(isPhoneWidth(412), true);
  assert.equal(isPhoneWidth(640), true);
  assert.equal(isPhoneWidth(641), false);
  assert.equal(isPhoneWidth(1600), false);
  assert.equal(isPhoneWidth('412'), true);
  assert.equal(isPhoneWidth(undefined), false);
});

test('phone is list-first, but a saved page wins', () => {
  assert.equal(phoneEntryPage(null), 'delivery');
  assert.equal(phoneEntryPage(undefined), 'delivery');
  assert.equal(phoneEntryPage('timeline'), 'timeline');
  assert.equal(phoneEntryPage('ticket'), 'ticket');
  // the first phone timeline is the story hierarchy, where the Jira keys live
  assert.equal(phoneEntryLanes(null), 'story');
  assert.equal(phoneEntryLanes('actor'), 'actor');
});

test('the hint line names the gestures the device actually has', () => {
  const phone = hintText('timeline', true);
  assert.match(phone, /pinch to zoom/);
  assert.match(phone, /hold for details/);
  assert.doesNotMatch(phone, /wheel|arrow keys/);
  const desktop = hintText('timeline', false);
  assert.match(desktop, /wheel to zoom/);
  assert.match(desktop, /arrows pan/);
  assert.match(hintText('delivery', true), /hold to pivot/);
  assert.match(hintText('delivery', false), /right-click/);
});

test('a sheet dismisses on a long drag or a quick flick, never upward', () => {
  assert.equal(shouldDismissSheet({ dy: 20, velocity: 0 }), false);
  assert.equal(shouldDismissSheet({ dy: -200, velocity: 2 }), false);
  assert.equal(shouldDismissSheet({ dy: 100, velocity: 0 }), true);
  assert.equal(shouldDismissSheet({ dy: 40, velocity: 0.6 }), true);
  assert.equal(shouldDismissSheet({ dy: 40, velocity: 0.1 }), false);
  assert.equal(sheetOffset(-10), '');
  assert.equal(sheetOffset(42), 'translateY(42px)');
});

/* ---- the stacked cards: every cell carries its own label ---------------- */

function openRow(overrides = {}) {
  return {
    object_id: 'PR:DEL/delivery-web#52',
    repo_short: 'delivery-web',
    number: 52,
    author: 'AlNg',
    state: 'open',
    ball: 'review',
    ball_label: 'review',
    phase: 'review',
    kind: 'pr_approved',
    waiting_since: '2026-11-13T12:07:00Z',
    waiting_hours: 2.9,
    age_hours: 16.4,
    blocked: true,
    dependents: 0,
    impact_hours: 0,
    commits: 2,
    ...overrides,
  };
}

test('the delivery table labels every cell for the phone card layout', () => {
  const html = deliveryTable({ rows: [openRow()] }, 'all');
  for (const label of ['status', 'waiting on', 'phase', 'waiting since', 'waiting', 'impact']) {
    assert.match(html, new RegExp(`data-label="${label}"`), `missing ${label}`);
  }
  // the key cell is the card heading, not a key-value row
  assert.match(html, /class="c-id" data-label=""/);
});

test('the ticket PR table and event list label their cells too', () => {
  const prs = prTable({
    prs: [{
      object_id: 'PR:DEL/delivery-web#52', number: 52, repo: 'DEL/delivery-web',
      state: 'merged', opened: '2026-10-29T14:06:00Z', cycle_hours: 27.7,
      review_rounds: 3, push_classes: { content: 2 }, censored: false, intervals: [],
    }],
  });
  for (const label of ['state', 'opened', 'cycle', 'rounds', 'push classes', 'lifecycle']) {
    assert.match(prs, new RegExp(`data-label="${label}"`), `missing ${label}`);
  }
  const events = eventsList({
    events: [{ ts: '2026-10-19T09:40:00Z', activity: 'created', actor: 'LaAd', attributes: {} }],
  });
  for (const label of ['when', 'activity', 'actor', 'attributes']) {
    assert.match(events, new RegExp(`data-label="${label}"`), `missing ${label}`);
  }
});

test('the rules drill-down labels its item / detail / measured cells', () => {
  const html = rulesMarkup({
    level: 'team',
    sample: { prs: 1, days: 90 },
    metrics: {
      cycle_time: {
        rows: [{ id: '#52', hours: 27.7, censored: false, tickets: ['DEL-197'], affected: true }],
      },
    },
    suggestions: [{
      id: 'cycle_time', rank: 1, severity: 'warning', metric: 'cycle_time',
      title: 'Cycle time is long', suggestion: 'Split the work', impact_hours: 10,
      confidence: 0.8, evidence: { median_hours: 27.7 }, scope: { kind: 'team' },
    }],
  });
  assert.match(html, /data-label="detail"/);
  assert.match(html, /data-label="measured"/);
  assert.match(html, /data-label=""/);
});

/* ---- source guards: the phone rules cannot leak into the desktop ------- */

test('prd/28: the phone shell is gated on one breakpoint', () => {
  const css = read('style.css');
  assert.match(css, /@media \(max-width: 640px\)/);
  // the ctx-row wrapper is transparent on desktop, so the flex order is the
  // pre-phone one (the desktop screenshot is byte-identical)
  assert.match(css, /\.ctx-row \{ display: contents; \}/);
  // the off-canvas controls do not exist on desktop
  assert.match(css, /\.nav-toggle \{ display: none; \}/);
  assert.match(css, /\.sheet-handle, \.sheet-close \{ display: none; \}/);
  // canvases own their horizontal gestures, but vertical drags scroll the page
  assert.match(css, /#timeline, #blocking, #queues \{ touch-action: pan-y; \}/);
  // tables stack with data-label cards
  assert.match(css, /content: attr\(data-label\)/);
  // no viewport-wide horizontal scrollbar is hidden to mask overflow
  assert.doesNotMatch(css, /html, body \{[^}]*overflow-x: hidden/);
});

test('prd/28: the phone wiring exists in the page source', () => {
  const html = read('index.html');
  assert.match(html, /id="nav-toggle"/);
  assert.match(html, /id="scrim"/);
  assert.match(html, /class="ctx-row"/);
  assert.match(html, /id="timeline-hint"/);
  assert.match(html, /id="delivery-hint"/);
  assert.match(html, /id="range-meta"/);

  const main = read('main.js');
  assert.match(main, /initMobile\(\{/);
  assert.match(main, /bindLongPress\(/);
  assert.match(main, /phoneEntryPage\(null\)/);
  assert.match(main, /phoneEntryLanes\(null\)/);
  assert.match(main, /hintText\('timeline', phone\)/);
  assert.match(main, /sheet-close/);
  assert.match(main, /classList\.toggle\('has-selection'/);

  // one-finger pan, two-finger pinch, tap and hold on both canvases
  for (const file of ['timeline.js', 'graph.js']) {
    const src = read(file);
    assert.match(src, /pointerdown/, `${file} has no touch handler`);
    assert.match(src, /pointerType === 'mouse'/, `${file} does not separate mouse from touch`);
    assert.match(src, /pointermove/, `${file} has no pan/pinch handler`);
    assert.match(src, /_pinchState\(\)/, `${file} has no pinch state`);
    assert.match(src, /450/, `${file} has no tap-and-hold delay`);
  }
  assert.match(read('timeline.js'), /GUTTER_PHONE/);
  assert.match(read('graph.js'), /resetView\(\)/);
  // the WIP chart has no pan/zoom, but its hover tooltip needs a touch path
  assert.match(read('queues.js'), /pointerdown/);
  assert.match(read('queues.js'), /_holdTimer/);
});

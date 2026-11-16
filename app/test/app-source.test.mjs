/* Source-level guards for behaviour that only exists in the wired page.
 *
 * The pure helpers are unit-tested; these assert the wiring the QA round 2
 * bugs came from is still present, so a refactor cannot quietly reintroduce
 * them. Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

test('B2: no view labels a PR with keys[0]', () => {
  for (const file of ['timeline.js', 'tables.js', 'main.js']) {
    assert.doesNotMatch(read(file), /keys\[0\]/, `${file} still uses keys[0]`);
  }
});

test('B2: the timeline takes its label key from the ticket census', () => {
  const timeline = read('timeline.js');
  assert.match(timeline, /pr\.ticket/);
  assert.match(timeline, /setTicketIndex/);
  assert.match(read('main.js'), /epicKeySet\(keyListsOf\(state\.objects\)\)/);
});

test('B3/B4/B5: the banner, queue scope and drill-down use the shared helpers', () => {
  const main = read('main.js');
  assert.match(main, /scopeBanner\(state\.scope, state\.page\)/);
  assert.match(main, /scopeForStream\(stream\)/);
  assert.match(main, /setQueueLevel\(queueLevelForNode\(node\)\)/);
});

test('B7: the timeline disclosure cannot paint over another view', () => {
  assert.match(read('main.js'), /if \(state\.page !== 'timeline'\) return;/);
});

test('B8: the level selects are reconciled from the restored state', () => {
  const main = read('main.js');
  assert.match(main, /function syncLevelSelects\(\)/);
  assert.match(main, /syncLevelSelects\(\);\n\}/);
  assert.match(main, /blocking\.value = state\.blockingLevel/);
  assert.match(main, /queue\.value = state\.queueLevel/);
});

test('NFR-2: events page newest-first and the cap is liftable', () => {
  const main = read('main.js');
  assert.match(main, /\/api\/events\?order=desc/);
  assert.match(main, /loadFullRange/);
  const html = read('index.html');
  assert.match(html, /id="load-all"/);
});

test('B6/G5: the timeframe follows the timeline window and All passes days', () => {
  const main = read('main.js');
  assert.match(main, /function noteTimelineView/);
  assert.match(main, /state\.timeframeCustom/);
  assert.match(main, /function presetWindow/);
  assert.match(main, /function allDays/);
  assert.match(main, /const days = timeframeDays\(\) \?\? allDays\(\)/);
  // the range is only painted from the timeline while the timeline is visible
  assert.match(main, /if \(state\.page === 'timeline'\) \{/);
  assert.match(read('index.html'), /id="tf-custom"/);
});

test('B9/B11/B12: stale selections and tabs cannot outlive their data', () => {
  const main = read('main.js');
  assert.match(main, /data\.rows\.find\(\(row\) => row\.object_id === selected\.object_id\)/);
  assert.match(main, /if \(!current \|\| !matchesScopeRow\(current\)\) state\.selection = null/);
  assert.match(main, /state\.tabPinned = true/);
  assert.match(main, /!state\.tabPinned/);
  assert.match(main, /timeline\.clearSelection\(\);\n  timeline\.setTicketIndex/);
});

test('B10: a zero-event timeline has an empty state and scopes are validated', () => {
  const main = read('main.js');
  assert.match(main, /function updateTimelineEmpty/);
  assert.match(main, /No events in this window\./);
  assert.match(main, /const valid = validateScope\(\)/);
  assert.match(main, /No events for scope/);
});

test('B13: status and tooltips are transient', () => {
  const main = read('main.js');
  assert.match(main, /function flashStatus/);
  assert.match(main, /flashStatus\('copied as Markdown'/);
  assert.match(read('queues.js'), /this\.hover = null;\n    if \(this\.tip\) this\.tip\.hidden = true;/);
  assert.match(read('graph.js'), /this\.hover = null;\n    if \(this\.tip\) this\.tip\.hidden = true;/);
});

test('B14/B18/B20: the design-language fixes are in the stylesheet', () => {
  const css = read('style.css');
  assert.match(css, /#honesty \{ color: var\(--text-secondary\); \}/);
  assert.match(css, /--control-h: 24px/);
  assert.match(css, /body\[data-density="S"\][^}]*--control-h: 22px/s);
  assert.match(css, /\.rule \{[^}]*padding: var\(--density-pad\)/s);
  assert.match(css, /\.table \.keys \{[^}]*font-family: var\(--font-mono\)/s);
});

test('B15/B16/B17/B19/B21: the deferred view fixes are wired', () => {
  const queues = read('queues.js');
  assert.match(queues, /export function wipTicks/);
  assert.match(queues, /WIP scale 0–/);
  assert.match(read('rules.js'), /export function personSignalNote/);
  assert.match(read('main.js'), /queueLevel: state\.queueLevel/);
  assert.match(read('rules.js'), /if \(key === 'target_hours'\) return fmtHours\(value\)/);
  const main = read('main.js');
  assert.match(main, /option\.setAttribute\('selected', 'selected'\)/);
  assert.match(main, /position', 'absolute', 'important'/);
});

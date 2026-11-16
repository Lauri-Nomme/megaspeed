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

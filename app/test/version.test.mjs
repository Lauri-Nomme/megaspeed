/* prd/32 — the build identity line the UI shows on every page.
 *
 * The label is a pure helper (app/version.js) so the exact strings are testable
 * without a DOM; the wiring guards below keep the meta strip, the /api/meta
 * fetch and the phone stylesheet from drifting away from it.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { versionLabel, versionTitle } from '../version.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/* ---- the label: tag · short SHA ---------------------------------------- */

test('the label is the tag and the short SHA, PRD-style', () => {
  assert.equal(versionLabel({ version: 'v0.9.0-3-g88f72bb', tag: 'v0.9.0', sha: '88f72bb' }),
    'v0.9.0 · 88f72bb');
  // the tagged commit itself: describe is just the tag, the SHA still shows
  assert.equal(versionLabel({ version: 'v0.9.0', tag: 'v0.9.0', sha: '88f72bb' }),
    'v0.9.0 · 88f72bb');
});

test('a pre-tag build shows the SHA alone, a baked release the tag alone', () => {
  assert.equal(versionLabel({ version: '88f72bb', tag: '', sha: '88f72bb' }), '88f72bb');
  assert.equal(versionLabel({ version: 'v0.9.0', tag: 'v0.9.0', sha: '' }), 'v0.9.0');
  // no split fields at all: fall back to the raw version
  assert.equal(versionLabel({ version: '0.9.0' }), '0.9.0');
  assert.equal(versionLabel({}), '');
  assert.equal(versionLabel(null), '');
});

test('the tooltip names where the identity came from', () => {
  const git = versionTitle({ version: 'v0.9.0-3-g88f72bb', build_time: '2026-11-18T00:52:13+00:00', source: 'git' });
  assert.match(git, /v0\.9\.0-3-g88f72bb/);
  assert.match(git, /git describe/);
  assert.match(git, /built 2026-11-18/);
  const baked = versionTitle({ version: 'v0.9.0', source: 'version-file' });
  assert.match(baked, /VERSION file/);
  assert.match(versionTitle(null), /build identity/);
});

/* ---- the wiring -------------------------------------------------------- */

test('the meta strip carries the version element on every page', () => {
  const html = read('index.html');
  assert.match(html, /id="app-version"/);
  // inside the sticky meta strip, which every page shares
  const meta = html.slice(html.indexOf('<div class="meta"'), html.indexOf('<!-- Timeline -->'));
  assert.match(meta, /id="app-version"/);
});

test('main.js fetches /api/meta once and renders it', () => {
  const main = read('main.js');
  assert.match(main, /import \{ versionLabel, versionTitle \} from '\/version\.js'/);
  assert.match(main, /async function loadVersion\(\)/);
  assert.match(main, /getJSON\('\/api\/meta'\)/);
  assert.match(main, /function renderVersion\(\)/);
  assert.match(main, /\$\('app-version'\)/);
  // boot calls it independently of the store, so a store-less server still
  // shows its build identity
  assert.match(main, /loadVersion\(\);\nload\(\)\.then/);
  // the Sources page lists it among "what this screen is made of"
  assert.match(main, /\['build', versionLabel\(state\.build\)/);
});

test('the version line is styled and stays visible on the phone layout', () => {
  const css = read('style.css');
  assert.match(css, /\.build \{ color: var\(--text-tertiary\)/);
  // the phone hides the "·" separators, not the version itself: no rule may
  // hide .build
  assert.doesNotMatch(css, /\.build[^{]*\{[^}]*display: none/s);
});

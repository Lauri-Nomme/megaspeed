/* prd/49 — `repo` as a first-class scope dimension.
 *
 * The backend half (the slice, the `repo=` filter, the 400) is pinned in
 * `tests/test_repo_scope.py`. This file pins the UI half: the scope is one more
 * `{kind, value}` in the existing plumbing, it narrows the same views the PRD
 * names, and the shareable URL carries it (both `?scope=repo:…` and a bare
 * `?repo=…`, the form the API uses).
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { SCOPE_KINDS, parseRoute, parseScope, serializeRoute } from '../route.js';
import {
  filteredPages, repoLabel, scopeBanner, scopeMeaning,
} from '../scope.js';
import { matchesScope } from '../tables.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const REPO = 'delivery-api';

test('repo is a real scope kind and its URL round-trips', () => {
  assert.ok(SCOPE_KINDS.includes('repo'));
  const scope = { kind: 'repo', value: REPO, label: REPO };
  assert.deepEqual(parseScope(`repo:${REPO}`), scope);
  // the shareable URL writes the API's own parameter (`repo=`), not `scope=`
  const href = serializeRoute({ tenant: 'del-combined', view: 'delivery', scope });
  assert.match(href, /repo=delivery-api/);
  assert.doesNotMatch(href, /scope=/);
  const parsed = parseRoute(href, '');
  assert.deepEqual(parsed.scope, scope);
  // the scope-style URL is accepted as well, so both links land on the view
  assert.deepEqual(parseRoute('?view=delivery&scope=repo%3Adelivery-api', '').scope, scope);
  assert.equal(parseRoute('?view=delivery', '').scope, null);
});

test('a repo scope narrows Queue, Blocking, Delivery, Rules and the Lab', () => {
  const pages = filteredPages({ kind: 'repo', value: REPO });
  for (const page of ['delivery', 'queue', 'blocking', 'overview', 'rules', 'policy', 'timeline']) {
    assert.ok(pages.has(page), `repo scope must filter ${page}`);
  }
  // Sources is provenance, not a PR view
  assert.equal(pages.has('sources'), false);
  assert.equal(scopeMeaning({ kind: 'repo', value: REPO }, 'rules'),
    'rules recomputed on delivery-api');
  assert.equal(scopeMeaning({ kind: 'repo', value: 'ENG/delivery-api' }, 'timeline'),
    'events in delivery-api');
  assert.equal(repoLabel('ENG/delivery-api'), 'delivery-api');
  // the banner says this view is filtered and what it means, in the same words
  const banner = scopeBanner({ kind: 'repo', value: REPO }, 'queue');
  assert.match(banner, /Scope repo delivery-api \(the delivery-api repo's open PRs and waits\)/);
  assert.match(banner, /this view \(queue view\) is filtered to it/);
  assert.match(banner, /filtered: overview, delivery list, timeline, blocking map, queue view, rules and policy lab/);
  assert.match(banner, /not filtered: sources/);
});

test('a repo scope matches the row repository, both forms', () => {
  const scope = { kind: 'repo', value: 'ENG/delivery-api' };
  assert.equal(matchesScope({ repo: 'ENG/delivery-api', repo_short: 'delivery-api' }, scope), true);
  assert.equal(matchesScope({ repo: 'ENG/delivery-web', repo_short: 'delivery-web' }, scope), false);
  // the short name alone is enough for a row that only carries that
  assert.equal(matchesScope({ repo_short: 'delivery-api' }, scope), true);
  // case-insensitive, like every other scope comparison
  assert.equal(matchesScope({ repo: 'eng/DELIVERY-API' }, scope), true);
});

test('the page offers the repo list, filters the timeline and refetches on change', () => {
  const main = read('main.js');
  // the option set is the store's own repos, never a dead-end guess
  assert.match(main, /function availableRepos\(\)/);
  assert.match(main, /object\.object_type === 'PR' && object\.repo/);
  assert.match(main, /repo \$\{repoLabel\(repo\)\}/);
  // a repo scope is a server filter, so it travels as `repo=`
  assert.match(main, /function repoQuery\(\)/);
  assert.match(main, /repo=\$\{encodeURIComponent\(state\.scope\.value\)\}/);
  // the four endpoints the PRD names take it
  for (const endpoint of ['/api/delivery', '/api/blocking', '/api/queues', '/api/insights', '/api/simulate']) {
    assert.ok(main.includes(endpoint), `${endpoint} is not requested`);
  }
  // and entering/leaving a repo scope refetches (the others filter client-side)
  assert.match(main, /const wasRepo = state\.scope\.kind === 'repo'/);
  assert.match(main, /if \(wasRepo \|\| nowRepo\) \{[\s\S]*ensureData\(state\.page\)/);
  // the timeline filters its events and objects by repo
  assert.match(main, /state\.scope\.kind === 'repo'[\s\S]*event\.repo/);
  assert.match(main, /if \(state\.scope\.kind === 'repo'\) \{\n    const repo = String\(state\.scope\.value\)/);
  // the scope control's label names the new dimension
  assert.match(read('index.html'), /person, queue or repo/);
});

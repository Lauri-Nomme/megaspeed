/* prd/80 — ticket titles resolved in the browser.
 *
 * The feature is mostly a set of prohibitions, so most of these tests are
 * prohibitions too:
 *
 *   - fields are exactly `summary` and `issuetype` — the JQL body, the
 *     bookmarklet source and the CSV/JSON parsers are all asserted against it;
 *   - no user field is ever requested (an assignee beside a pseudonym is the
 *     de-anonymisation map prd/02 exists to prevent);
 *   - the only network targets are the configured Jira site and
 *     `api.atlassian.com` — a recording `fetch` proves a same-origin URL is
 *     never even built;
 *   - titles never reach an export: the Markdown renderers are called with a
 *     primed cache and the summary string is absent;
 *   - titles render as text, never markup, and the toggle-off path returns
 *     before it reads one.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  TITLE_FIELDS, TITLE_TTL_DAYS, coverage, csvRecords, formatAge, importLine,
  normalizeTitlesPref, normalizeType, parseCsv, parseJson, parseTitles,
  readTitlesMessage, typeGlyph, typeLabel, titlesEnabled,
} from '../titles.js';
import {
  cacheKey, clearTitles, getTitles, memoryStore, mergeTitles, openCache, stats,
  titlesOf,
} from '../titles-cache.js';
import {
  GATEWAY_ORIGIN, TOKEN_TTL_SECONDS, bookmarkletCode, bookmarkletSnippet,
  discoverCloudId, fetchTitles, gatewayUrl, isAllowedTitleUrl, probeSample,
  siteBase, testBasicGateway, testGateway, titleBatches, titleJql,
} from '../titles-probe.js';
import {
  TITLE_LONGPRESS_MS, annotateDetailTitle, hydrateTitles, listenTitlesMessage,
  probeMessage, titlesMarkup, wireTitlePopover,
} from '../titles-panel.js';
import { ROUTE_VIEWS, parseRoute, serializeRoute } from '../route.js';
import { deliveryMarkdown } from '../tables.js';
import { ticketMarkdown } from '../ticket.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const TITLES_SOURCE = read('titles.js');
const CACHE_SOURCE = read('titles-cache.js');
const PROBE_SOURCE = read('titles-probe.js');
const PANEL_SOURCE = read('titles-panel.js');
const MAIN_SOURCE = read('main.js');

const SUMMARY = 'parser crash on an empty queue';

/** A fetch Response stand-in: status, content type and a text body. */
function jsonResponse(body, status = 200, type = 'application/json') {
  return {
    status,
    headers: { get: (name) => (name === 'content-type' ? type : null) },
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
  };
}

/* =========================================================== the allowlist */

test('prd/80 rule 1: there is no title route and no same-origin URL', () => {
  assert.deepEqual(TITLE_FIELDS, ['summary', 'issuetype']);
  // Not one `"/api/…"` literal in the resolver — there is nothing to call.
  for (const source of [TITLES_SOURCE, CACHE_SOURCE, PROBE_SOURCE]) {
    assert.doesNotMatch(source, /['"`]\/api\//, 'a title module built a same-origin URL');
  }
  // The client never names the app origin as a fetch target.
  assert.equal(isAllowedTitleUrl('/api/delivery', 'acme.atlassian.net'), false);
  assert.equal(isAllowedTitleUrl(`${GATEWAY_ORIGIN}.evil.example/ex/jira/x`, 'acme.atlassian.net'), false);
  assert.equal(isAllowedTitleUrl('https://evil.example/ex/jira/x', 'acme.atlassian.net'), false);
  assert.equal(isAllowedTitleUrl('https://acme.atlassian.net.evil.example/x', 'acme.atlassian.net'), false);
  // … while the two legitimate targets are allowed.
  assert.equal(isAllowedTitleUrl(`${GATEWAY_ORIGIN}/ex/jira/x/rest/api/3/myself`, ''), true);
  assert.equal(isAllowedTitleUrl('https://acme.atlassian.net/_edge/tenant_info', 'acme.atlassian.net'), true);
  assert.equal(isAllowedTitleUrl('https://acme.atlassian.net/rest/api/3/search/jql', 'https://acme.atlassian.net/'), true);
});

test('prd/80 rule 1: every probe call targets only the Jira site or the gateway', async () => {
  const calls = [];
  const fake = async (url) => {
    calls.push(String(url));
    return jsonResponse({ id: 'cloud-1', issues: [] });
  };
  await discoverCloudId('acme.atlassian.net', fake);
  await testGateway({ cloudId: 'cloud-1', token: 'tok' }, fake);
  await testBasicGateway({ cloudId: 'cloud-1', token: 'YmFzZTY0' }, fake);
  await fetchTitles({ cloudId: 'cloud-1', token: 'tok', keys: ['DEMO-1'] }, fake);
  assert.ok(calls.length >= 4);
  for (const url of calls) {
    assert.ok(
      url.startsWith(`${GATEWAY_ORIGIN}/`) || url.startsWith('https://acme.atlassian.net/'),
      `a title request went somewhere else: ${url}`,
    );
    assert.ok(!/\/\/localhost|\/\/127\.0\.0\.1|^\/api\//.test(url), `same-origin request: ${url}`);
  }
});

/* ============================================================== the fields */

test('prd/80 rule 2: exactly summary + issuetype in every request shape', async () => {
  assert.deepEqual(TITLE_FIELDS, ['summary', 'issuetype']);
  // CSV: only the three import fields survive, whatever the export carried.
  const csv = parseCsv('Issue key,Summary,Issue type,Status,Assignee\n'
    + 'DEMO-347,"empty queue, retried",Bug,In Progress,Alice Example\n');
  assert.equal(csv.rows.length, 1);
  assert.deepEqual(Object.keys(csv.rows[0]), ['key', 'summary', 'type']);
  assert.deepEqual(csv.rows[0], { key: 'DEMO-347', summary: 'empty queue, retried', type: 'bug' });
  assert.equal(JSON.stringify(csv.rows).includes('Alice'), false);
  assert.equal(JSON.stringify(csv.rows).includes('In Progress'), false);

  // JQL is keys only — no field name can leak into it.
  const jql = titleJql(['DEMO-1', 'DEMO-2']);
  assert.equal(jql, 'key in (DEMO-1,DEMO-2)');
  for (const forbidden of ['status', 'assignee', 'priority', 'description', 'comment', 'reporter']) {
    assert.equal(jql.includes(forbidden), false);
  }

  // The gateway body: `fields` is the allowlist, and the body carries nothing else.
  const bodies = [];
  const fake = async (url, init) => {
    bodies.push(JSON.parse(init.body));
    return jsonResponse({ issues: [] });
  };
  await fetchTitles({ cloudId: 'c', token: 't', keys: ['DEMO-1'] }, fake);
  assert.deepEqual(bodies[0].fields, TITLE_FIELDS);
  assert.deepEqual(Object.keys(bodies[0]).sort(), ['fields', 'jql', 'maxResults']);
});

test('prd/80 rule 3: no user field is ever requested or stored', () => {
  // The parsers drop an assignee column entirely (the test above), and the
  // cache value is exactly three fields.
  assert.deepEqual(Object.keys({ summary: 's', type: 'bug', fetched_at: 1 }),
    ['summary', 'type', 'fetched_at']);
  const code = bookmarkletCode({ site: 'acme.atlassian.net', targetOrigin: 'https://ms.example' });
  for (const forbidden of ['assignee', 'reporter', 'creator', 'user', 'displayName', 'email']) {
    assert.equal(code.includes(forbidden), false, `the bookmarklet mentions ${forbidden}`);
  }
  // The probe prints status + size, never a person.
  const sample = probeSample({
    status: 200, type: 'application/json', body: '{"displayName":"Alice Example"}',
  });
  assert.equal(sample.includes('Alice'), false);
  assert.match(sample, /^HTTP 200 · application\/json · \d+ bytes$/);
});

/* ================================================================ the CSV */

test('CSV: quoted commas, quoted newlines and doubled quotes survive', () => {
  const csv = 'Issue key,Summary,Issue type\r\n'
    + 'DEMO-1,"empty queue, retried",Bug\r\n'
    + 'DEMO-2,"line one\nline two",Story\r\n'
    + 'DEMO-3,"he said ""stop""",Task\r\n';
  const { rows, skipped } = parseCsv(csv);
  assert.equal(skipped, 0);
  assert.deepEqual(rows.map((row) => row.key), ['DEMO-1', 'DEMO-2', 'DEMO-3']);
  assert.equal(rows[0].summary, 'empty queue, retried');
  assert.equal(rows[1].summary, 'line one\nline two');
  assert.equal(rows[2].summary, 'he said "stop"');
  assert.deepEqual(rows.map((row) => row.type), ['bug', 'story', 'task']);
  // the tokenizer itself: a trailing empty line is not a record
  assert.deepEqual(csvRecords('a,b\n1,2\n'), [['a', 'b'], ['1', '2']]);
});

test('CSV: the Issue Key / Issue Type spellings are accepted', () => {
  const { rows } = parseCsv('Issue Key,Summary,Issue Type\nDEMO-9,Hello,Sub-task\n');
  assert.deepEqual(rows, [{ key: 'DEMO-9', summary: 'Hello', type: 'subtask' }]);
});

test('CSV: a malformed line is counted and skipped, never a partial row', () => {
  const { rows, skipped } = parseCsv('Issue key,Summary,Issue type\n'
    + 'DEMO-1,ok,Bug\n'
    + ',no key,Bug\n'
    + 'not-a-key,nope,Task\n'
    + 'DEMO-2,,Task\n'
    + 'DEMO-3,"still, quoted",Story\n');
  assert.deepEqual(rows.map((row) => row.key), ['DEMO-1', 'DEMO-3']);
  assert.equal(skipped, 3);
  for (const row of rows) assert.ok(row.summary);
});

test('CSV: a missing column is a stated miss, never a half import', () => {
  const { rows, skipped, error } = parseCsv('Issue key,Reporter\nDEMO-1,Alice\nDEMO-2,Bob\n');
  assert.deepEqual(rows, []);
  assert.equal(skipped, 2);
  assert.equal(error, 'no key/summary column');
});

/* =============================================================== the JSON */

test('JSON: all three shapes reduce to the same rows', () => {
  const rest = JSON.stringify({
    issues: [
      { key: 'DEMO-1', fields: { summary: 'rest shape', issuetype: { name: 'Story' } } },
      { key: 'DEMO-2', fields: { summary: 'a bug', issuetype: { name: 'Bug' } } },
    ],
  });
  const flat = JSON.stringify([
    { key: 'DEMO-1', summary: 'rest shape', type: 'Story' },
    { key: 'DEMO-2', summary: 'a bug', type: 'bug' },
  ]);
  const envelope = JSON.stringify({
    type: 'megaspeed.titles', v: 1,
    rows: [
      { key: 'DEMO-1', summary: 'rest shape', type: 'Story' },
      { key: 'DEMO-2', summary: 'a bug', type: 'Bug' },
    ],
  });
  const expected = [
    { key: 'DEMO-1', summary: 'rest shape', type: 'story' },
    { key: 'DEMO-2', summary: 'a bug', type: 'bug' },
  ];
  assert.deepEqual(parseJson(rest).rows, expected);
  assert.deepEqual(parseJson(flat).rows, expected);
  assert.deepEqual(parseJson(envelope).rows, expected);
  // malformed JSON is an empty import, not a throw
  assert.deepEqual(parseJson('{oops').rows, []);
  assert.equal(parseJson('{oops').error, 'not JSON');
  // a row without a key or a summary is counted, not stored
  const partial = parseJson(JSON.stringify([{ key: 'DEMO-1', summary: '' }, { key: 'DEMO-2', summary: 'x' }]));
  assert.deepEqual(partial.rows.map((row) => row.key), ['DEMO-2']);
  assert.equal(partial.skipped, 1);
});

test('parseTitles picks the shape from the text', () => {
  assert.equal(parseTitles('[{"key":"DEMO-1","summary":"json","type":"bug"}]').rows.length, 1);
  assert.equal(parseTitles('Issue key,Summary,Issue type\nDEMO-1,csv,Bug\n').rows[0].summary, 'csv');
  assert.deepEqual(parseTitles('').rows, []);
});

/* ============================================================ the vocabulary */

test('normalizeType is tolerant of case, spaces and Sub-task spellings', () => {
  const cases = {
    Epic: 'epic', 'STORY ': 'story', story: 'story', Task: 'task', Bug: 'bug',
    'Sub-task': 'subtask', Subtask: 'subtask', 'SUB_TASK': 'subtask', 'sub task': 'subtask',
    Spike: 'other', '': 'other', null: 'other', 'Technical task': 'other',
  };
  for (const [input, expected] of Object.entries(cases)) {
    assert.equal(normalizeType(input), expected, `normalizeType(${String(input)})`);
  }
  assert.equal(typeLabel('subtask'), 'Sub-task');
  assert.equal(typeLabel('other'), 'Other');
});

test('typeGlyph: Jira palette, 14 px, decorative, no title text', () => {
  const palette = {
    epic: '#6554C0', story: '#36B37E', task: '#4BADE8', bug: '#E5493A',
    subtask: '#4BADE8', other: '#6B778C',
  };
  for (const [type, colour] of Object.entries(palette)) {
    const svg = typeGlyph(type);
    assert.match(svg, /width="14" height="14"/);
    assert.match(svg, /aria-hidden="true"/);
    assert.ok(svg.includes(colour), `${type} is missing ${colour}`);
    assert.match(svg, new RegExp(`data-type="${type}"`));
  }
  // an unknown name still draws a glyph (grey square), never an empty string
  assert.match(typeGlyph('mystery'), /#6B778C/);
});

/* ================================================================= the cache */

test('cacheKey is per tenant and per key', () => {
  assert.equal(cacheKey('demo-a', 'DEMO-1'), 'demo-a|DEMO-1');
  assert.notEqual(cacheKey('demo-a', 'DEMO-1'), cacheKey('demo-b', 'DEMO-1'));
});

test('merge is idempotent and never blanks a row the input omits', async () => {
  const store = memoryStore();
  const first = await mergeTitles('demo-a', [
    { key: 'DEMO-1', summary: 'one', type: 'bug' },
    { key: 'DEMO-2', summary: 'two', type: 'story' },
  ], store, { now: 1_000 });
  assert.deepEqual(first, { imported: 2, unchanged: 0, skipped: 0 });

  // re-importing the same rows changes nothing (and does not fake freshness)
  const second = await mergeTitles('demo-a', [
    { key: 'DEMO-1', summary: 'one', type: 'bug' },
  ], store, { now: 9_999 });
  assert.deepEqual(second, { imported: 0, unchanged: 1, skipped: 0 });
  const rows = await titlesOf('demo-a', store);
  assert.equal(rows.find((row) => row.key === 'DEMO-1').fetched_at, 1_000);

  // a partial export overwrites only what it carries
  await mergeTitles('demo-a', [
    { key: 'DEMO-2', summary: 'two, fixed', type: 'story' },
  ], store, { now: 2_000 });
  const after = await titlesOf('demo-a', store);
  assert.equal(after.length, 2);
  assert.equal(after.find((row) => row.key === 'DEMO-1').summary, 'one');
  assert.equal(after.find((row) => row.key === 'DEMO-2').summary, 'two, fixed');
  // the value is exactly the three allowed fields
  for (const row of after) {
    assert.deepEqual(Object.keys(row).sort(), ['fetched_at', 'key', 'summary', 'type']);
  }
});

test('the cache is per tenant: reads, stats and Forget stay inside one', async () => {
  const store = memoryStore();
  await mergeTitles('demo-a', [{ key: 'DEMO-1', summary: 'a one', type: 'bug' }], store, { now: 10 });
  await mergeTitles('demo-b', [{ key: 'DEMO-1', summary: 'b one', type: 'epic' }], store, { now: 20 });

  const a = await getTitles('demo-a', ['DEMO-1'], store);
  const b = await getTitles('demo-b', ['DEMO-1'], store);
  assert.equal(a.get('DEMO-1').summary, 'a one');
  assert.equal(b.get('DEMO-1').summary, 'b one');

  const statsA = await stats('demo-a', store);
  assert.equal(statsA.total, 1);
  assert.equal(statsA.oldest, 10);
  assert.equal(statsA.newest, 10);
  assert.deepEqual(statsA.byType, { bug: 1 });

  assert.equal(await clearTitles('demo-a', store), 1);
  assert.equal((await titlesOf('demo-a', store)).length, 0);
  assert.equal((await titlesOf('demo-b', store)).length, 1, 'Forget touched another tenant');
});

test('a store-less browser degrades to nothing, never a throw', async () => {
  assert.deepEqual(await getTitles('demo-a', ['DEMO-1'], null), new Map());
  assert.deepEqual(await mergeTitles('demo-a', [{ key: 'DEMO-1', summary: 'x' }], null),
    { imported: 0, unchanged: 0, skipped: 1 });
  assert.equal(await clearTitles('demo-a', null), 0);
  // openCache has an injectable factory, so the node path is the memory store.
  const store = await openCache({ factory: null });
  assert.equal(typeof store.get, 'function');
});

/* ============================================================ the transports */

test('siteBase / gatewayUrl normalise the two configured origins', () => {
  assert.equal(siteBase('acme.atlassian.net'), 'https://acme.atlassian.net');
  assert.equal(siteBase('https://acme.atlassian.net/'), 'https://acme.atlassian.net');
  assert.equal(siteBase('javascript:alert(1)'), '');
  assert.equal(gatewayUrl('cloud-1', '/rest/api/3/myself'),
    'https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/myself');
});

test('discoverCloudId reads the CORS-open tenant info', async () => {
  const calls = [];
  const fake = async (url) => {
    calls.push(url);
    return jsonResponse({ id: 'cloud-abc', cloudId: 'x' });
  };
  const found = await discoverCloudId('acme.atlassian.net', fake);
  assert.deepEqual(found, {
    ok: true, cloudId: 'cloud-abc', status: 200, error: '',
  });
  assert.equal(calls[0], 'https://acme.atlassian.net/_edge/tenant_info');
  // a site we cannot build a URL for is not requested at all
  const none = await discoverCloudId('', fake);
  assert.equal(none.ok, false);
  assert.equal(calls.length, 1);
});

test('testGateway: 200, 401, 429 and a CORS rejection are all stated, never thrown', async () => {
  const ok = await testGateway({ cloudId: 'c', token: 'tok' },
    async () => jsonResponse({ accountId: 'x', displayName: 'Alice Example' }));
  assert.equal(ok.ok, true);
  assert.equal(ok.status, 200);
  assert.match(ok.sample, /^HTTP 200 · application\/json · \d+ bytes$/);
  assert.equal(ok.sample.includes('Alice'), false);
  assert.equal(ok.error, '');

  const unauthorized = await testGateway({ cloudId: 'c', token: 'bad' },
    async () => jsonResponse({ errorMessages: ['Unauthorized'] }, 401));
  assert.equal(unauthorized.ok, false);
  assert.equal(unauthorized.status, 401);
  assert.equal(unauthorized.error, 'unauthorized');

  const limited = await testGateway({ cloudId: 'c', token: 'tok' },
    async () => jsonResponse({}, 429));
  assert.equal(limited.ok, false);
  assert.equal(limited.error, 'rate-limited');

  const cors = await testGateway({ cloudId: 'c', token: 'tok' },
    async () => { throw new TypeError('Failed to fetch'); });
  assert.deepEqual(cors, { ok: false, status: 0, sample: '', error: 'cors-or-network' });
  assert.match(probeMessage('Test gateway', cors), /CORS blocked/);

  // no cloud id: nothing is called
  let called = false;
  const missing = await testGateway({ cloudId: '', token: 'tok' },
    async () => { called = true; return jsonResponse({}); });
  assert.equal(missing.error, 'no-cloud-id');
  assert.equal(called, false);

  // Basic is the same call with the other scheme
  let header = '';
  await testBasicGateway({ cloudId: 'c', token: 'YmFzZTY0' }, async (url, init) => {
    header = init.headers.Authorization;
    return jsonResponse({});
  });
  assert.equal(header, 'Basic YmFzZTY0');
});

test('fetchTitles batches at 50, catches 401/429, and keeps what arrived', async () => {
  const keys = Array.from({ length: 120 }, (_, i) => `DEMO-${i + 1}`);
  assert.equal(titleBatches(keys).length, 3);
  assert.equal(titleBatches(keys)[2].length, 20);
  assert.deepEqual(titleBatches(keys).flat(), keys);

  const bodies = [];
  const fake = async (url, init) => {
    bodies.push(JSON.parse(init.body));
    const jqlKeys = JSON.parse(init.body).jql.match(/[A-Z][A-Z0-9_]*-\d+/g) || [];
    return jsonResponse({
      issues: jqlKeys.map((key) => ({
        key, fields: { summary: `title of ${key}`, issuetype: { name: 'Story' } },
      })),
    });
  };
  const result = await fetchTitles({ cloudId: 'c', token: 't', keys }, fake);
  assert.equal(result.ok, true);
  assert.equal(result.rows.length, 120);
  assert.equal(result.batches, 3);
  assert.deepEqual(result.rows[0], { key: 'DEMO-1', summary: 'title of DEMO-1', type: 'story' });
  assert.equal(bodies[0].maxResults, 50);

  const unauthorized = await fetchTitles({ cloudId: 'c', token: 't', keys },
    async () => jsonResponse({}, 401));
  assert.equal(unauthorized.error, 'unauthorized');
  assert.deepEqual(unauthorized.rows, []);

  const limited = await fetchTitles({ cloudId: 'c', token: 't', keys },
    async () => jsonResponse({}, 429));
  assert.equal(limited.error, 'rate-limited');

  const cors = await fetchTitles({ cloudId: 'c', token: 't', keys },
    async () => { throw new TypeError('Failed to fetch'); });
  assert.equal(cors.error, 'cors-or-network');

  // an empty key list is an answer, not a request
  let called = false;
  const empty = await fetchTitles({ cloudId: 'c', token: 't', keys: [] },
    async () => { called = true; return jsonResponse({}); });
  assert.equal(empty.batches, 0);
  assert.equal(called, false);
});

test('the token is a one-hour paste, stated as such', () => {
  assert.equal(TOKEN_TTL_SECONDS, 3600);
});

test('bookmarkletCode: snapshot — two fields, same-origin, the target origin, v:1', () => {
  const code = bookmarkletCode({
    site: 'acme.atlassian.net',
    targetOrigin: 'https://ms.example',
    keys: ['DEMO-1', 'DEMO-2', 'not-a-key'],
  });
  assert.ok(code.startsWith('javascript:'));
  // exactly the two fields
  assert.match(code, /var T=\["summary","issuetype"\]/);
  assert.match(code, /fields:T/);
  for (const forbidden of ['status', 'assignee', 'priority', 'description', 'comment', 'reporter']) {
    assert.equal(code.includes(forbidden), false, `the snippet mentions ${forbidden}`);
  }
  // the session the operator already has; no token, no Bearer
  assert.match(code, /credentials:"same-origin"/);
  assert.equal(code.includes('Bearer'), false);
  assert.equal(code.includes('Authorization'), false);
  // the panel it opens, and the origin-checked message it sends
  assert.match(code, /window\.open\(O\+"\/\?view=titles"/);
  assert.match(code, /postMessage\(\{type:"megaspeed\.titles",v:1,rows:rows\},O\)/);
  assert.match(code, /https:\/\/acme\.atlassian\.net/);
  assert.match(code, /"https:\/\/ms\.example"/);
  // the invalid key never reaches the JQL
  assert.equal(code.includes('not-a-key'), false);
  // the console fallback is the same code without the scheme
  const plain = bookmarkletCode({ site: 'acme.atlassian.net', targetOrigin: 'https://ms.example' });
  assert.equal(bookmarkletSnippet({ site: 'acme.atlassian.net', targetOrigin: 'https://ms.example' }),
    plain.replace(/^javascript:/, ''));
});

test('the bookmarklet receiver: origin and payload shape, or nothing', () => {
  const data = {
    type: 'megaspeed.titles', v: 1,
    rows: [{ key: 'DEMO-1', summary: 'hello', type: 'Bug' }],
  };
  const expected = 'https://acme.atlassian.net';
  const good = readTitlesMessage({ origin: expected, data }, expected);
  assert.deepEqual(good.rows, [{ key: 'DEMO-1', summary: 'hello', type: 'bug' }]);

  // an unexpected origin is ignored, not logged
  assert.equal(readTitlesMessage({ origin: 'https://evil.example', data }, expected), null);
  assert.equal(readTitlesMessage({ origin: '', data }, expected), null);
  // the wrong envelope version or type is ignored
  assert.equal(readTitlesMessage({ origin: expected, data: { ...data, v: 2 } }, expected), null);
  assert.equal(readTitlesMessage({ origin: expected, data: { ...data, type: 'x' } }, expected), null);
  // a row whose key is not a ticket key is a malformed payload
  assert.equal(readTitlesMessage({
    origin: expected, data: { ...data, rows: [{ key: 'DROP TABLE', summary: 'x' }] },
  }, expected), null);

  // the listener wires it up, and the site may be a getter (configured later)
  const listeners = {};
  const win = {
    addEventListener: (name, fn) => { listeners[name] = fn; },
    removeEventListener: (name) => { delete listeners[name]; },
  };
  let site = '';
  const seen = [];
  listenTitlesMessage(win, { site: () => site, onRows: (rows) => seen.push(rows) });
  listeners.message({ origin: expected, data });
  assert.equal(seen.length, 0, 'a message arrived before the Jira site was configured');
  site = 'acme.atlassian.net';
  listeners.message({ origin: expected, data });
  assert.equal(seen.length, 1);
});

/* ============================================================ the browser DOM */

/* A minimal fake tree — only the handful of methods hydration touches. The
 * app's other tests read source instead of faking a DOM; here the toggle-off
 * *behaviour* is the rule under test, so a tiny tree is worth it. */

function fakeDoc() {
  const doc = {
    createElement: (tag) => fakeElement(doc, tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text), parentElement: null }),
  };
  return doc;
}

function fakeMatches(el, selector) {
  return selector.split(',').some((part) => {
    const sel = part.trim();
    if (sel === '[data-ticket]') return typeof (el.dataset && el.dataset.ticket) === 'string';
    if (sel === '[data-title-resolved]') return Boolean(el.dataset && el.dataset.titleResolved);
    if (sel.startsWith('.')) return String(el.className).split(/\s+/).includes(sel.slice(1));
    return false;
  });
}

function fakeDescendants(node) {
  const out = [];
  for (const child of node.children || []) {
    if (child.nodeType) continue;
    out.push(child, ...fakeDescendants(child));
  }
  return out;
}

function fakeElement(doc, tag) {
  const el = {
    ownerDocument: doc,
    tagName: String(tag).toUpperCase(),
    className: '',
    children: [],
    dataset: {},
    attrs: {},
    parentElement: null,
    _text: '',
    get textContent() {
      return this.children.length
        ? this.children.map((child) => child.textContent || '').join('')
        : this._text;
    },
    set textContent(value) {
      this._text = String(value);
      this.children = [];
    },
    get nextSibling() {
      const parent = this.parentElement;
      if (!parent) return null;
      const at = parent.children.indexOf(this);
      return at < 0 ? null : parent.children[at + 1] || null;
    },
    setAttribute(name, value) { this.attrs[String(name)] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    removeAttribute(name) { delete this.attrs[name]; },
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      return child;
    },
    insertBefore(child, ref) {
      child.parentElement = this;
      const at = ref ? this.children.indexOf(ref) : -1;
      if (at < 0) this.children.push(child);
      else this.children.splice(at, 0, child);
      return child;
    },
    removeChild(child) {
      const at = this.children.indexOf(child);
      if (at >= 0) this.children.splice(at, 1);
      child.parentElement = null;
      return child;
    },
    querySelector(selector) {
      return fakeDescendants(this).find((node) => fakeMatches(node, selector)) || null;
    },
    querySelectorAll(selector) {
      return fakeDescendants(this).filter((node) => fakeMatches(node, selector));
    },
    closest(selector) {
      let node = this;
      while (node) {
        if (fakeMatches(node, selector)) return node;
        node = node.parentElement;
      }
      return null;
    },
  };
  return el;
}

function fakeTicket(key = 'DEMO-347', { line = true } = {}) {
  const doc = fakeDoc();
  const root = fakeElement(doc, 'main');
  const cell = fakeElement(doc, 'td');
  const anchor = fakeElement(doc, 'a');
  anchor.className = 'ticket-link';
  anchor.dataset.ticket = key;
  if (line) anchor.dataset.ticketLine = '1';
  anchor.attrs.title = `open the ticket detail page for ${key}`;
  cell.appendChild(anchor);
  root.appendChild(cell);
  return { doc, root, cell, anchor };
}

function titleLineIn(cell) {
  return cell.children.find((child) => String(child.className).includes('ticket-title'));
}

test('hydration: annotates the anchor and adds the opt-in second line as text', () => {
  const { root, cell, anchor } = fakeTicket();
  const titles = new Map([['DEMO-347', { summary: SUMMARY, type: 'bug', fetched_at: Date.now() }]]);
  assert.equal(hydrateTitles(root, { enabled: true, titles }), 1);
  // the native tooltip is `KEY — summary`
  assert.equal(anchor.getAttribute('title'), `DEMO-347 — ${SUMMARY}`);
  assert.equal(anchor.getAttribute('data-title-resolved'), '1');
  // the muted second line under the key, icon first, summary as a text node
  const line = titleLineIn(cell);
  assert.ok(line, 'no second line was added');
  assert.equal(line.children[0].className, 'ticket-title-icon');
  assert.match(line.children[0].innerHTML, /#E5493A/);
  assert.ok(line.children.some((child) => child.nodeType === 3
    && child.textContent === SUMMARY), 'the summary must be a text node, never markup');
});

test('hydration: an inline table cell keeps the key only (no second line)', () => {
  const { root, cell, anchor } = fakeTicket('DEMO-1', { line: false });
  const titles = new Map([['DEMO-1', { summary: SUMMARY, type: 'story' }]]);
  assert.equal(hydrateTitles(root, { enabled: true, titles }), 1);
  assert.equal(anchor.getAttribute('title'), `DEMO-1 — ${SUMMARY}`);
  assert.equal(titleLineIn(cell), undefined, 'row heights and column widths must not move');
});

test('unconfigured: no cache rows means exactly today’s markup', () => {
  const { root, cell, anchor } = fakeTicket();
  assert.equal(hydrateTitles(root, { enabled: true, titles: new Map() }), 0);
  assert.equal(anchor.getAttribute('title'), 'open the ticket detail page for DEMO-347');
  assert.equal(anchor.getAttribute('data-title-resolved'), null);
  assert.equal(cell.children.length, 1);
});

test('prd/80 rule 8: the toggle-off path returns before reading a title', () => {
  const { root, cell, anchor } = fakeTicket();
  hydrateTitles(root, {
    enabled: true,
    titles: new Map([['DEMO-347', { summary: SUMMARY, type: 'bug' }]]),
  });
  assert.ok(titleLineIn(cell));

  // A cache that would answer — and would be caught answering.
  let reads = 0;
  const spy = {
    size: 1,
    get(key) {
      reads += 1;
      return { summary: SUMMARY, type: 'bug' };
    },
  };
  assert.equal(hydrateTitles(root, { enabled: false, titles: spy }), 0);
  assert.equal(reads, 0, 'the off path read a title');
  assert.equal(titleLineIn(cell), undefined);
  assert.equal(anchor.getAttribute('title'), 'open the ticket detail page for DEMO-347');
  assert.equal(root.textContent.includes(SUMMARY), false);

  // The page's own switch also returns before hydrating with data.
  const start = MAIN_SOURCE.indexOf('function renderTitles()');
  const disabledCall = MAIN_SOURCE.indexOf('hydrateTitles(document.getElementById', start);
  const guard = MAIN_SOURCE.slice(start, disabledCall + 200);
  assert.match(guard, /if \(!state\.titlesEnabled\)/);
  assert.match(guard, /enabled: false/);
  assert.match(guard, /return;/);
});

test('the ticket page subtitle: summary under the h1, and off removes it', () => {
  const doc = fakeDoc();
  const head = fakeElement(doc, 'div');
  const h1 = fakeElement(doc, 'h1');
  h1.textContent = 'DEMO-347';
  head.appendChild(h1);

  const node = annotateDetailTitle(h1, {
    key: 'DEMO-347', title: { summary: '<script>alert(1)</script>', type: 'epic' },
  });
  assert.ok(node, 'no subtitle');
  assert.equal(node.className, 'ticket-title-sub');
  assert.equal(head.children[1], node);
  // the summary is text: the angle brackets are characters, not markup
  assert.ok(node.children.some((child) => child.nodeType === 3
    && child.textContent === '<script>alert(1)</script>'));
  assert.equal(node.textContent.includes('<script>alert(1)</script>'), true);

  // off removes it; no title means no node at all (exactly today's markup)
  assert.equal(annotateDetailTitle(h1, {
    key: 'DEMO-347', title: { summary: SUMMARY }, enabled: false,
  }), null);
  assert.equal(head.querySelector('.ticket-title-sub'), null);
  assert.equal(annotateDetailTitle(h1, { key: 'DEMO-347', title: null }), null);
  assert.equal(head.children.length, 1);
  // a title for a different key is not this page's subtitle
  assert.equal(annotateDetailTitle(h1, {
    key: 'DEMO-999', title: { summary: SUMMARY, type: 'bug' },
  }), null);
});

/* ============================================================ the panel */

test('titlesMarkup: status, import, bookmarklet, token probe and Forget', () => {
  const html = titlesMarkup({
    enabled: true,
    tenant: 'demo-a',
    site: 'acme.atlassian.net',
    cloudId: 'cloud-1',
    coverageText: '412 of 600 keys have a title',
    keyCount: 600,
    oldestText: '41 days old',
    newestText: 'today',
    stale: true,
    bookmarkletHref: 'javascript:(function(){})();',
  });
  assert.match(html, /412 of 600 keys have a title/);
  assert.match(html, /oldest 41 days old · newest today/);
  assert.match(html, /older than 30 days/);
  assert.match(html, /id="titles-file"/);
  assert.match(html, /id="titles-paste"/);
  assert.match(html, /id="titles-import"/);
  assert.match(html, /id="titles-import-line"/);
  assert.match(html, /drag me to your bookmarks bar/);
  assert.match(html, /href="javascript:\(function\(\)\{\}\)\(\);"/);
  assert.match(html, /id="titles-copy-snippet"/);
  assert.match(html, /id="titles-cloud"/);
  assert.match(html, /id="titles-discover"/);
  assert.match(html, /type="password"/);
  assert.match(html, /id="titles-test"/);
  assert.match(html, /id="titles-test-basic"/);
  assert.match(html, /id="titles-fetch"/);
  assert.match(html, /id="titles-expiry"/);
  assert.match(html, /Forget titles/);
  assert.match(html, /nothing refreshes it in the browser/i);
  // the toggle is mirrored, and the token field never carries a value
  assert.match(html, /id="titles-toggle" aria-pressed="true"/);
  assert.match(html, /id="titles-token" value=""/);
  // off renders the same panel with the switch pressed off
  assert.match(titlesMarkup({ enabled: false }), /id="titles-toggle" aria-pressed="false"/);
});

test('importLine and coverage are the panel’s one honest sentence each', () => {
  assert.equal(importLine({ imported: 412, skipped: 3, unchanged: 1886 }),
    'imported 412 · skipped 3 · 1886 unchanged');
  assert.equal(importLine({}), 'imported 0 · skipped 0 · 0 unchanged');
  const view = coverage(
    [{ key: 'DEMO-1' }, { key: 'DEMO-3' }],
    ['DEMO-1', 'DEMO-2', 'DEMO-3', 'DEMO-4', 'demo-1'],
  );
  assert.deepEqual(
    { covered: view.covered, total: view.total, text: view.text },
    { covered: 2, total: 4, text: '2 of 4 keys have a title' },
  );
  assert.equal(coverage([], []).text, '0 of 0 keys have a title');
});

test('formatAge and the TTL phrase', () => {
  assert.equal(formatAge(0), 'today');
  assert.equal(formatAge(1), '1 day old');
  assert.equal(formatAge(41), '41 days old');
  assert.equal(formatAge(null), '');
  assert.equal(TITLE_TTL_DAYS, 30);
});

test('the toggle preference is a closed set, on by default', () => {
  assert.equal(normalizeTitlesPref('off'), 'off');
  assert.equal(normalizeTitlesPref('ON'), 'on');
  assert.equal(normalizeTitlesPref('maybe'), null);
  assert.equal(titlesEnabled(null), true);
  assert.equal(titlesEnabled('off'), false);
  assert.equal(TITLE_LONGPRESS_MS, 350);
});

/* ========================================================= no server transit */

test('prd/80 rule 4/6: the resolver does not import the store or a view', () => {
  for (const source of [TITLES_SOURCE, CACHE_SOURCE, PROBE_SOURCE]) {
    assert.doesNotMatch(source, /from ['"][^'"]*(store|metrics|tables|ticket|aging|rules|verdict|trend|sprints|policy)\.js['"]/);
  }
  // titles.js imports nothing at all; the cache imports only the vocabulary.
  assert.doesNotMatch(TITLES_SOURCE, /^import /m);
  assert.match(CACHE_SOURCE, /from '\.\/titles\.js'/);
  assert.match(PROBE_SOURCE, /from '\.\/titles\.js'/);
  // The export renderers do not know titles exist.
  for (const file of ['tables.js', 'ticket.js', 'aging.js', 'rules.js', 'verdict.js', 'trend.js']) {
    assert.doesNotMatch(read(file), /titles\.js|titles-cache|hydrateTitles/,
      `${file} reached for a title`);
  }
});

test('prd/80 rule 6: an export never carries a summary, even with a primed cache', async () => {
  // A cache that WOULD answer for DEMO-347 — the export path must not ask it.
  const store = memoryStore();
  await mergeTitles('demo-a', [{ key: 'DEMO-347', summary: SUMMARY, type: 'bug' }], store);
  const title = await getTitles('demo-a', ['DEMO-347'], store);
  assert.equal(title.get('DEMO-347').summary, SUMMARY);

  const row = {
    object_id: 'PR:DEMO/widget#1', number: 1, repo_short: 'widget', tickets: ['DEMO-347'],
    ticket: 'DEMO-347', state: 'open', ball: 'review', ball_label: 'review',
    waiting_since: '2026-11-13T12:07:00Z', waiting_hours: 90, age_hours: 200,
    blocked: false, dependents: 0, impact_hours: 0, commits: 2, author: 'AlNg',
  };
  const markdown = deliveryMarkdown([row], 'snapshot 2026-11-27', 'https://ms.example');
  assert.equal(markdown.includes(SUMMARY), false);

  const ticket = ticketMarkdown({
    key: 'DEMO-347',
    answer: 'it waited',
    hours_mode: 'business',
    snapshot: '2026-11-27T00:00:00Z',
    story: { state: 'done', assignee: 'LaNo', created: '2026-10-01T00:00:00Z' },
    segments: { rows: [] },
    prs: [],
  }, 'https://ms.example');
  assert.equal(ticket.includes(SUMMARY), false);

  // … and the copy handlers in the page do not hydrate in between.
  for (const id of ['#delivery-copy', '#ticket-copy', '#rules-copy', '#policy-copy']) {
    const at = MAIN_SOURCE.indexOf(id);
    assert.ok(at > 0, `${id} handler missing`);
    const chunk = MAIN_SOURCE.slice(at, at + 900);
    assert.doesNotMatch(chunk, /hydrateTitles|state\.titles/, `${id} touched a title`);
  }
});

/* ================================================================== routing */

test('prd/80: Titles is a route, and ?titles=off round-trips like ?theme=', () => {
  assert.ok(ROUTE_VIEWS.includes('titles'));
  assert.deepEqual(parseRoute('?view=titles', '').view, 'titles');
  assert.equal(parseRoute('?titles=off', '').titles, 'off');
  assert.equal(parseRoute('?titles=on', '').titles, 'on');
  assert.equal(parseRoute('?titles=maybe', '').titles, null);
  // on is the default, so it costs no characters (old links stay byte-identical)
  assert.equal(serializeRoute({ titles: 'on' }), '');
  assert.equal(serializeRoute({ titles: 'off' }), '?titles=off');
  assert.equal(serializeRoute({ tenant: 'demo-a', view: 'titles', titles: 'off' }),
    '?tenant=demo-a&view=titles&titles=off');
});

test('the popover: a summary is text, and closing leaves no title in the DOM', () => {
  const doc = fakeDoc();
  doc.body = fakeElement(doc, 'body');
  doc.documentElement = { clientWidth: 1200 };
  const listeners = {};
  doc.addEventListener = (name, fn) => { (listeners[name] ||= []).push(fn); };
  doc.removeEventListener = () => {};
  const popover = wireTitlePopover(doc, {
    isEnabled: () => true,
    titleFor: (key) => (key === 'DEMO-347'
      ? { summary: '<b>parser crash</b>', type: 'bug', fetched_at: Date.now() }
      : null),
  });
  const anchor = fakeElement(doc, 'a');
  anchor.dataset.ticket = 'DEMO-347';
  anchor.getBoundingClientRect = () => ({ left: 10, bottom: 20, width: 80 });

  for (const handler of listeners.mouseover) handler({ target: anchor });
  const pop = doc.body.children[0];
  assert.ok(pop, 'no popover element');
  assert.equal(pop.hidden, false);
  assert.equal(pop.getAttribute('role'), 'dialog');
  // the summary is text: the angle brackets are characters, not an element
  assert.equal(pop.textContent.includes('<b>parser crash</b>'), true);
  const summaryEl = pop.children.find((node) => String(node.className).includes('title-pop-summary'));
  assert.ok(summaryEl, 'no summary line');
  assert.equal(summaryEl.textContent, '<b>parser crash</b>');
  assert.equal(summaryEl.children.length, 0, 'the summary must not be parsed as markup');
  assert.match(pop.textContent, /from your Jira session · not stored/);
  assert.equal(anchor.getAttribute('aria-expanded'), 'true');

  // closing hides it AND drops the text, so "off" leaves nothing to read
  popover.close();
  assert.equal(pop.hidden, true);
  assert.equal(pop.textContent, '');
  assert.equal(anchor.getAttribute('aria-expanded'), 'false');
});

test('the page wires the toggle, the hydrator and the panel', () => {
  assert.match(MAIN_SOURCE, /data-page="titles"|title: 'Titles'/);
  assert.match(MAIN_SOURCE, /titles: \{ title: 'Titles'/);
  assert.match(MAIN_SOURCE, /renderTitlesChrome\(\);\n  saveUI\(\);/);
  assert.match(MAIN_SOURCE, /renderDeliveryDetail\(\);\n  \/\/ prd\/80/);
  assert.match(MAIN_SOURCE, /wireTitlePopover\(document/);
  assert.match(MAIN_SOURCE, /listenTitlesMessage\(window/);
  assert.match(MAIN_SOURCE, /hydrateTitles\(document\.getElementById\('main'\)/);
  // the panel lives in the view markup
  assert.match(read('index.html'), /data-page="titles">Titles</);
  assert.match(read('index.html'), /id="page-titles"/);
  assert.match(read('index.html'), /id="titles-toggle"/);
  // and the stylesheet owns the popover, the second line and the toggle
  const css = read('style.css');
  assert.match(css, /\.title-pop \{/);
  assert.match(css, /\.ticket-title \{/);
  assert.match(css, /\.ticket-title-sub \{/);
  assert.match(css, /\.titles-toggle \{/);
  // the second line is opt-in on the epic page's ticket list only
  assert.match(read('ticket.js'), /data-ticket-line="1"/);
  // no inline table cell (the delivery table's row heights/widths stay put)
  assert.doesNotMatch(read('tables.js'), /data-ticket-line/);
  assert.doesNotMatch(read('aging.js'), /data-ticket-line/);
});

/* prd/67 W-05: the carry-over card states both scopes.
 *
 * "7 stories committed but not done by their sprint's close" was correct for
 * the window's cohort (one sprint closed in 28 days) and read as a store total
 * (106 sprints). The card now names the window's sprint count in the title and
 * puts the whole store beside the window count as one evidence fact.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  evidenceLabel, rulesMarkdown, rulesMarkup, storeScopeItem,
} from '../rules.js';

const WINDOW_TITLE = '7 of 36 stories committed in the 1 sprint closed in this '
  + '28.0-day window were not done by its close';

function carrySuggestion(evidence = {}) {
  return {
    id: 'jira-carry-over', rank: 1, severity: 'critical', metric: 'carry_over',
    title: WINDOW_TITLE,
    suggestion: 'Plan from a refined backlog and finish what you commit.',
    impact_hours: 70, confidence: 0.5,
    evidence: {
      carried_over: 7,
      stories_in_a_sprint: 36,
      window_sprints: 1,
      store_carried_over: 412,
      store_sprints: 106,
      late: 5,
      never: 2,
      ...evidence,
    },
    scope: { kind: 'team' },
  };
}

function page(suggestion) {
  return {
    level: 'team', sample: { prs: 0, days: 28 }, hours_mode: 'business',
    as_of: '2026-11-17T00:00:00Z', note: '', metrics: {},
    jira: { available: true }, suggestions: [suggestion],
  };
}

test('the whole store sits next to the window count, as one fact', () => {
  const html = rulesMarkup(page(carrySuggestion()));
  // the window title is untouched by the renderer
  assert.ok(html.includes(WINDOW_TITLE));
  // one combined fact, not two snake_case chips
  assert.match(html, /<dt>the whole store<\/dt>\s*<dd class="mono">412 carried over across 106 sprints<\/dd>/);
  assert.doesNotMatch(html, /store_carried_over/);
  assert.doesNotMatch(html, /store_sprints/);
  assert.ok(html.includes('sprints closed in this window'));
  // the window count comes first, exactly once
  assert.equal((html.match(/carried over across/g) || []).length, 1);
  const windowAt = html.indexOf('<dt>carried over</dt>');
  const storeAt = html.indexOf('<dt>the whole store</dt>');
  assert.ok(windowAt >= 0 && storeAt > windowAt,
    'the store fact follows the window count');
});

test('the export carries the same two scopes', () => {
  const markdown = rulesMarkdown(page(carrySuggestion()));
  assert.ok(markdown.includes(WINDOW_TITLE));
  assert.match(markdown, /the whole store 412 carried over across 106 sprints/);
});

test('a store with no closed sprint renders no store scope', () => {
  const html = rulesMarkup(page(carrySuggestion({
    store_carried_over: null, store_sprints: null,
  })));
  assert.doesNotMatch(html, /the whole store/);
  assert.ok(html.includes(WINDOW_TITLE));
  assert.equal(storeScopeItem(null, null), null);
});

test('singular and plural store scopes read in words', () => {
  assert.deepEqual(storeScopeItem(1, 1), {
    key: 'store_carried_over',
    label: 'the whole store',
    value: '1 carried over across 1 sprint',
  });
  assert.equal(storeScopeItem(412, 106).value,
    '412 carried over across 106 sprints');
  assert.equal(evidenceLabel('window_sprints'), 'sprints closed in this window');
});

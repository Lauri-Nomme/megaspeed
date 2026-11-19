/* prd/44 DES-5 — the calendar fingerprint reaches the copied numbers.
 *
 * A saved report or a "Copy as Markdown" must name the calendar that priced it,
 * otherwise a vacation/holiday edit makes the figure unreproducible. The server
 * carries ``calendar_id`` on every analysis payload; these pin that the
 * Markdown exporters render it.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { entityMarkdown } from '../tables.js';
import { policyMarkdown, provenanceLine } from '../policy.js';

const ROW = {
  object_id: 'PR:DEL/web#52', repo: 'DEL/web', repo_short: 'web', number: 52,
  tickets: ['DEL-197'], ticket: 'DEL-197', author: 'AlNg', state: 'open',
  ball: 'review', ball_label: 'review', phase: 'review',
  waiting_since: '2026-11-13T12:07:00Z', waiting_hours: 40,
  age_hours: 80, blocked: false, dependents: 0, impact_hours: 0,
};

test('the PR copy names the calendar that priced its hours', () => {
  const text = entityMarkdown({ row: ROW }, 'business', '', 'abc123def456');
  assert.match(text, /business hours/);
  assert.match(text, /calendar abc123def456/);
});

test('a wall-clock copy carries the same id (never silently absent)', () => {
  const text = entityMarkdown({ row: ROW }, 'wall-clock', '', 'none');
  assert.match(text, /calendar none/);
});

test('the policy provenance line and Markdown carry the calendar id', () => {
  const data = {
    hours_mode: 'business', calendar_id: 'abc123def456',
    provenance: 'calibrated from 120 merged PRs', seed: 7,
    baseline: { predicted: { spread: { replications: 5 } } },
    calibration: { queueing: {} },
  };
  assert.match(provenanceLine(data), /calendar abc123def456/);
  const markdown = policyMarkdown({
    ...data,
    baseline: {
      ok: true,
      predicted: {
        cycle_hours: {}, p90_hours: {}, flow_efficiency: {}, throughput_work_per_day: {},
        throughput_per_day: {}, lead_hours: {}, spread: { replications: 5 },
      },
    },
    validation: { status: 'within tolerance', percent_off: 4.2 },
  });
  assert.match(markdown, /calendar abc123def456/);
});

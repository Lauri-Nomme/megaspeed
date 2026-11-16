/* prd/22: the stacked-cascade rule's evidence reads as prose, not snake_case.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { evidenceLabel, evidenceValue, ruleRow } from '../rules.js';

test('prd/22: stack evidence labels are words, not column names', () => {
  assert.equal(evidenceLabel('re_review_hours'), 're-review');
  assert.equal(evidenceLabel('critical_path_hours'), 'critical path');
  assert.equal(evidenceLabel('depth_median'), 'median depth');
  assert.equal(evidenceLabel('depth_max'), 'max depth');
  assert.equal(evidenceLabel('stacked_prs'), 'stacked PRs');
  // the generic fallbacks still apply to everything else
  assert.equal(evidenceLabel('median_hours'), 'median');
});

test('prd/22: hours take the impact unit, counts stay counts', () => {
  assert.equal(evidenceValue('re_review_hours', 14), '≈14 h');
  assert.equal(evidenceValue('critical_path_hours', 3.5), '≈3 h 30 min');
  assert.equal(evidenceValue('rebases', 2), '2');
  assert.equal(evidenceValue('depth_max', 3), '3');
  assert.equal(evidenceValue('depth_median', 1.5), '1.5');
});

test('prd/22: the rule row renders the stacked-cascade evidence', () => {
  const html = ruleRow({
    id: 'stacked-cascade',
    rank: 3,
    metric: 'stacked_cascade',
    severity: 'warning',
    title: '2 stacked PRs; a lower-PR update triggered 2 rebases and 14 h of re-review up-stack',
    suggestion: 'Merge stacks bottom-up',
    impact_hours: 14,
    confidence: 0.1,
    evidence: {
      stacked_prs: 2,
      rebases: 2,
      re_review_hours: 14,
      depth_median: 1.5,
      depth_max: 2,
      critical_path_hours: 14,
    },
  });
  assert.match(html, /re-review/);
  assert.match(html, /critical path/);
  assert.match(html, /median depth/);
  assert.match(html, /≈14 h at stake/);
  assert.doesNotMatch(html, /re_review_hours/);
});

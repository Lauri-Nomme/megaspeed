/* Story-level rework in the UI (prd/51).
 *
 * The panel is the answer to "how much rework is hiding in the stories?":
 * reopened / failed verification / review bounces / approved-but-never-merged,
 * each row a `#ticket/KEY` link with its count and hours. The tests pin the
 * pure markup (no browser), including the group totals and the empty group.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { fmtHours } from '../format.js';
import { REWORK_GROUPS, reworkMarkdown, reworkPanel } from '../rework.js';
import { rulesMarkdown, rulesMarkup } from '../rules.js';

const REWORK = {
  ok: true,
  available: true,
  scope: 'every story in the store (not the Rules window)',
  basis: 'reopened = done → ready flips; a bounce is priced to its recovery',
  count: 5,
  hours: 66,
  reopened: {
    label: 'reopened after done',
    count: 1,
    stories: 1,
    hours: 30,
    rows: [{ key: 'DEL-153', count: 1, hours: 30, open: false }],
  },
  failed_verification: {
    label: 'failed verification',
    count: 1,
    stories: 1,
    hours: 4,
    rows: [{ key: 'DEL-132', count: 1, hours: 4 }],
  },
  review_bounces: {
    label: 'review bounces',
    count: 2,
    stories: 1,
    hours: 14,
    rows: [{ key: 'DEL-181', count: 2, hours: 14 }],
  },
  approved_unmerged: {
    label: 'approved but never merged',
    count: 1,
    stories: 1,
    hours: 18,
    // the DEL-161 shape: both approved, the bot declined it later
    rows: [{
      key: 'DEL-161', number: 34, repo_short: 'delivery-web', count: 1,
      hours: 18, state: 'declined', approved_by: ['DaKo', 'FrMo'],
      closed_by: 'BiBi', bot_closed: true,
    }],
  },
};

test('the panel renders one group per signal, even at zero', () => {
  const html = reworkPanel(REWORK, { base: '' });
  assert.match(html, /id="story-rework"/);
  assert.equal((html.match(/data-rework-section=/g) || []).length, REWORK_GROUPS.length);
  assert.match(html, /data-rework-section="reopened"/);
  assert.match(html, /data-rework-section="failed_verification"/);
  assert.match(html, /data-rework-section="review_bounces"/);
  assert.match(html, /data-rework-section="approved_unmerged"/);
  assert.match(html, /data-rework-total>5 signals/);
  // a zero group stays visible and says so instead of vanishing
  const withEmpty = reworkPanel({
    ...REWORK,
    failed_verification: {
      label: 'failed verification', count: 0, stories: 0, hours: 0, rows: [],
    },
  });
  assert.match(withEmpty, /none in this store/);
  assert.match(withEmpty, /data-rework-section="failed_verification"/);
});

test('each row is a ticket link with its count and hours', () => {
  const html = reworkPanel(REWORK, { base: 'http://host/' });
  for (const [key, count, hours] of [
    ['DEL-153', 1, 30], ['DEL-132', 1, 4], ['DEL-181', 2, 14], ['DEL-161', 1, 18],
  ]) {
    assert.match(html, new RegExp(`href="http://host/#ticket/${key}"`));
    assert.match(html, new RegExp(`data-rework-row="${key}"`));
    assert.match(html, new RegExp(`data-rework-count="${count}"`));
    // the hours are the shared formatter, never a bespoke string
    assert.ok(html.includes(fmtHours(hours)), `${key} hours ${fmtHours(hours)}`);
  }
  // a multi-bounce story shows ×2, the orphan shows its PR number
  assert.match(html, /×2/);
  assert.match(html, /#34/);
  assert.match(html, /delivery-web/);
});

test('the orphan row names the approvals and the bot that closed it', () => {
  const html = reworkPanel(REWORK);
  assert.match(html, /approved by DaKo \+ FrMo/);
  assert.match(html, /closed by the bot BiBi/);
});

test('a store without the ticket layer has no panel', () => {
  assert.equal(reworkPanel(null), '');
  assert.equal(reworkPanel({ available: false }), '');
});

test('the group totals are the payload counts, not the rendered rows', () => {
  const html = reworkPanel(REWORK);
  assert.match(html, /data-rework-group-count="review_bounces">2/);
  assert.match(html, /data-rework-group-hours="review_bounces"/);
  assert.ok(html.includes(fmtHours(14)));
  assert.match(html, /data-rework-group-count="reopened">1/);
});

test('the Rules page carries the rework panel, on screen and in the export', () => {
  const data = {
    level: 'team', suggestions: [], metrics: {}, rework: REWORK,
    sample: { prs: 0, days: 0 }, note: 'fixture',
    hours_mode: 'business', as_of: '2026-02-02T09:00:00Z',
  };
  const html = rulesMarkup(data, { base: '' });
  assert.match(html, /id="story-rework"/);
  assert.match(html, /href="#ticket\/DEL-153"/);
  assert.match(html, /href="#ticket\/DEL-161"/);
  const markdown = rulesMarkdown(data, '');
  assert.match(markdown, /## Story rework \(5 signals/);
  assert.match(markdown, /\[DEL-153\]\(#ticket\/DEL-153\) ×1/);
});

test('the Markdown export keeps the same rows as ticket links', () => {
  const lines = reworkMarkdown(REWORK, 'http://host/');
  const text = lines.join('\n');
  assert.match(text, /## Story rework \(5 signals/);
  assert.match(text, /\[DEL-153\]\(http:\/\/host\/#ticket\/DEL-153\) ×1/);
  assert.match(text, /\[DEL-181\]\(http:\/\/host\/#ticket\/DEL-181\) ×2/);
  assert.match(text, /\[DEL-161\]\(http:\/\/host\/#ticket\/DEL-161\) #34/);
  assert.deepEqual(reworkMarkdown(null), []);
});

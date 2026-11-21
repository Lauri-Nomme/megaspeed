/* prd/60 — probabilistic throughput forecast ("how many, by when").
 *
 * The card is pure markup, so these tests pin the honesty rules without a
 * browser:
 *   - the **interval** is the headline (`22–30 of 30`), never P50 alone;
 *   - the P50 tick is proportional (22/27/30 → 62.5 % of the interval);
 *   - `--warning` only when even the optimistic edge misses (`p85 < target`),
 *     and never red;
 *   - a refusal renders the exact sentence and **no numbers** (no bar, no
 *     interval, no zero);
 *   - a 2-item set delegates and renders prd/52's named rows instead;
 *   - a target change re-reads the payload's pre-computed quantiles — no
 *     interpolation, no client-side simulation;
 *   - the phone layout is three lines + a disclosure and a 32 px bar: no axis,
 *     no chart, no horizontal scroll.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  forecastBar, forecastCard, forecastRiskLine, forecastTargetView, intervalLabel,
} from '../forecast.js';
import { atRiskMarkup, sprintStrip } from '../sprints.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/* ------------------------------------------------------------------ fixtures */

function forecastPayload(overrides = {}) {
  return {
    ok: true,
    available: true,
    as_of: '2026-11-18T00:10:44Z',
    method: 'Method: 10,000 resamples of 13 weeks of stories delivered (mean 6.5, sd 2.6, CV 0.41). Not points — points and logged hours do not track time here (r ≈ −0.05).',
    caveat: 'Both numbers are the 85 % edge: "by" uses P85 because a date is a promise; "how many" uses P15 for the same reason. A count is capped at the target, so a P85 of "30" can hide a 29 % chance of reaching 30 — the p(reach) line says which it is.',
    throughput: { basis: 'stories delivered / week', n: 13, mean: 6.46, sd: 2.65, cv: 0.41, values: [2, 3, 7, 8, 6, 7, 5, 7, 10, 4, 8, 5, 12] },
    assess: { available: true, reasons: [], warnings: [], minimum_weeks: 8 },
    request: { n: 30, periods: 4, by: '2026-12-27', target: 'sprint:4', horizon_weeks: 26, confidence: 'P15–P85' },
    forecast: {
      how_many: { p15: 22, p50: 27, p85: 30 },
      uncapped: { p15: 22, p50: 27, p85: 32 },
      p_reach_target: 0.29,
      p_reach_curve: [
        { n: 0, p: 1 }, { n: 20, p: 1 }, { n: 22, p: 0.95 },
        { n: 26, p: 0.78 }, { n: 27, p: 0.62 }, { n: 29, p: 0.45 },
        { n: 30, p: 0.29 }, { n: 31, p: 0.14 }, { n: 32, p: 0.05 },
      ],
      when: { p50_weeks: 5, p85_weeks: 6, p95_weeks: 7, p50_date: '2027-01-03', p85_date: '2027-01-10', unfinished_share: 0, start: '2026-11-30' },
      trials: 10000, seed: 1234567, capped_at: 30, delivered_now: 0, periods: 4,
      headline: '22–30 of 30 · by 27 Dec',
    },
    adjust: {
      delivered_now: 0, remaining: 30,
      at_risk: [
        { kind: 'stuck-on', key: 'DEL-176', pr: '#60', holder: 'FrMo', hours: 144, label: 'stuck on a person' },
        { kind: 'done-but-unmerged', key: 'DEL-167', pr: '#53', holder: 'CaOs + FrMo', hours: 168, label: 'Done in Jira, PR unmerged' },
      ],
      at_risk_count: 2,
      how_many_if_the_at_risk_merge: { p15: 22, p50: 27, p85: 30 },
      how_many_if_they_do_not: { p15: 21, p50: 26, p85: 29 },
      ceiling_if_they_do_not: 29, doubtful: 1,
      delivered_if_the_blocked_items_hold: 29,
    },
    delegate: null,
    note: null,
    ...overrides,
  };
}

function refusalPayload(code, sentence) {
  return {
    ok: true,
    available: false,
    assess: { available: false, reasons: [{ code, sentence, minimum: 8 }], warnings: [], minimum_weeks: 8 },
    throughput: { basis: 'stories delivered / week', n: 3, values: [1, 2, 1] },
    request: { n: 30, periods: 2, by: '2026-11-27' },
    forecast: null,
    adjust: null,
    delegate: null,
    method: '',
    caveat: '',
    note: sentence,
  };
}

const OVERLOAD = 'WIP rose 6 → 17 while throughput did not follow — Little\'s Law says cycle time will grow, so a throughput forecast would be optimistic. Fix the queue first (prd/61).';
const SPARSE = 'Not enough history to forecast: 3 weeks observed, 8 needed.';
const ERRATIC = 'Throughput is erratic (CV 0.74): the range would be so wide it would look like a forecast and behave like a guess.';

/* --------------------------------------------------------------------- tests */

test('the headline is the interval, never a point estimate', () => {
  const html = forecastCard(forecastPayload());
  assert.match(html, /class="forecast-headline">22–30 of 30 · by 27 Dec</);
  assert.match(html, /P50 27 · 29 % chance all 30 · 85 % by 10 Jan · 10,000 resamples · 13 weeks/);
  // the P50 is context, never the headline
  assert.doesNotMatch(html, /class="forecast-headline">P50/);
});

test('the P50 tick is proportional across the interval (22/27/30 → 62.5 %)', () => {
  const view = forecastTargetView(forecastPayload().forecast, 30);
  const bar = forecastBar(view, 30);
  assert.match(bar, /data-p50-pos="62\.50"/);
  // the interval span is the whole bar, and the target hairline is at its edge
  assert.match(bar, /class="forecast-span" style="left:0%;width:100%"/);
  assert.match(bar, /class="forecast-target-hair" style="left:100\.00%"/);
  assert.match(bar, /<span style="left:0\.00%">22<\/span>/);
  assert.match(bar, /<span style="left:62\.50%">27<\/span>/);
  assert.match(bar, /<span style="left:100\.00%">30<\/span>/);
});

test('warning colour only when even the optimistic edge misses', () => {
  const ok = forecastCard(forecastPayload());
  assert.match(ok, /data-forecast-warn="false"/);
  assert.doesNotMatch(ok, /forecast-card warn/);
  // p85 32 < target 33: neither edge reaches the ask
  const missed = forecastCard(forecastPayload(), { target: 33 });
  assert.match(missed, /forecast-card warn/);
  assert.match(missed, /data-forecast-warn="true"/);
  // red is never used: no critical colour class on the card
  assert.doesNotMatch(missed, /critical/);
});

test('the target re-renders from the pre-computed quantiles (no interpolation)', () => {
  const view = forecastTargetView(forecastPayload().forecast, 26);
  assert.deepEqual(view, { n: 26, p15: 22, p50: 26, p85: 26, p_reach: 0.78 });
  const html = forecastCard(forecastPayload(), { target: 26 });
  assert.match(html, /class="forecast-headline">22–26 of 26/);
  assert.match(html, /P50 26 · 78 % chance all 26/);
  // the target control carries the value that produced it
  assert.match(html, /value="26"[^>]*data-forecast-target-input/);
  // a target beyond the sampled range has no made-up p: the curve is the truth
  assert.equal(forecastTargetView(forecastPayload().forecast, 99).p_reach, null);
});

test('every interval carries n, p(reach), the method and the seed', () => {
  const html = forecastCard(forecastPayload());
  assert.match(html, /13 weeks/);
  assert.match(html, /29 % chance all 30/);
  assert.match(html, /Method: 10,000 resamples of 13 weeks/);
  assert.match(html, /seed 1234567/);
  assert.match(html, /data-forecast-seed="1234567"/);
  // the second honesty sentence is present, verbatim (85 % edge)
  assert.match(html, /Both numbers are the 85 % edge/);
});

test('the at-risk rows name the item, the holder and the wait', () => {
  assert.equal(
    forecastRiskLine({ kind: 'stuck-on', pr: '#60', holder: 'FrMo', hours: 144 }),
    '#60 stuck on FrMo · 18 d 0 h',
  );
  const html = forecastCard(forecastPayload());
  assert.match(html, /What the model can&#39;t see|What the model can't see/);
  assert.match(html, /#60 stuck on FrMo/);
  assert.match(html, /delivered if the blocked items hold:\s*<b>29<\/b>/);
});

test('a sparse refusal renders the exact sentence and no numbers', () => {
  const html = forecastCard(refusalPayload('sparse', SPARSE));
  assert.match(html, /data-forecast-state="refused"/);
  assert.match(html, /data-forecast-numbers="none"/);
  assert.match(html, new RegExp(SPARSE.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.doesNotMatch(html, /data-forecast-bar/);
  assert.doesNotMatch(html, /forecast-headline/);
  assert.doesNotMatch(html, /P50/);
  // never a zero dressed up as an interval
  assert.doesNotMatch(html, /0–0/);
});

test('an erratic refusal states the CV and shows no interval', () => {
  const html = forecastCard(refusalPayload('erratic', ERRATIC));
  assert.match(html, /Throughput is erratic \(CV 0\.74\)/);
  assert.doesNotMatch(html, /data-forecast-bar/);
  assert.doesNotMatch(html, /forecast-overline[\s\S]*forecast-headline/);
});

test('an overload refusal hands off to prd/61 with the exact copy', () => {
  const html = forecastCard(refusalPayload('overload', OVERLOAD));
  assert.match(html, /WIP rose 6 → 17 while throughput did not follow/);
  assert.match(html, /Fix the queue first \(prd\/61\)\./);
  assert.match(html, /data-forecast-handoff="prd61"/);
  assert.match(html, /See the stability strip \(prd\/61\)/);
  // the queue veto carries no forecast numbers
  assert.doesNotMatch(html, /data-forecast-bar/);
  assert.doesNotMatch(html, /chance all/);
});

test('a 2-item set delegates to prd/52 and renders the named rows', () => {
  const html = forecastCard({
    ...forecastPayload(),
    forecast: null,
    adjust: null,
    delegate: {
      to: 'prd/52',
      reason: 'a 2-item set is a risk question, not a throughput question',
      rows: [
        { kind: 'stuck-on', key: 'DEL-176', pr: '#60', holder: 'FrMo', hours: 144, label: 'stuck on a person' },
      ],
    },
  });
  assert.match(html, /data-forecast-state="delegate"/);
  assert.match(html, /a 2-item set is a risk question, not a throughput question/);
  assert.match(html, /#60 stuck on FrMo/);
  assert.match(html, /data-forecast-handoff="prd52"/);
  assert.doesNotMatch(html, /data-forecast-bar/);
  assert.doesNotMatch(html, /of 2<\/h2>/);
});

test('prd/52\'s block has the one "Will it land?" entry point', () => {
  const html = atRiskMarkup({
    at_risk: {
      available: true, sprint_id: 3, count: 2, stories: 30, done_by_end: 28,
      points_at_risk: 8, rows: [
        { key: 'DEL-176', reason: 'stuck-on', who: ['FrMo'], points: 3, waiting_hours: 144, detail: { prs: [60] } },
        { key: 'DEL-161', reason: 'approved-unmerged', who: ['AaAa'], points: 5, waiting_hours: 27, detail: { prs: [34], approved_by: ['RrRr'] } },
      ],
    },
  });
  assert.match(html, /data-forecast-open="3"/);
  assert.match(html, />Will it land\?</);
});

test('the card sits at the top of the sprint strip (one component, two entry points)', () => {
  const sprintPayload = {
    available: true, count: 1, active: 3,
    sprints: [{
      sprint_id: 3, state: 'active', open: true,
      band_start: '2026-10-08T09:00:00Z', band_end: '2026-11-17T02:25:45Z',
      planned_end: '2026-11-27', story_count: 30, done_by_end: 25,
      done_late: 0, never_done: 2, unmerged: 3, points: 125,
      stories: ['DEL-120'], carried_in: [], carried_out: [],
    }],
    scope: { stories: 1, in_sprint: 1, out_of_sprint: 0, unplanned: [] },
    carry_over: { count: 0, rows: [], late: 0 }, delivery: {},
    at_risk: { available: true, sprint_id: 3, count: 0, rows: [], stories: 30, done_by_end: 25, points_at_risk: 0 },
  };
  const html = sprintStrip(sprintPayload, { forecast: forecastPayload() });
  assert.match(html, /id="forecast-card"/);
  assert.ok(html.indexOf('id="forecast-card"') < html.indexOf('class="sprint-strip"'));
  // a sprint row is still a row; the card did not replace the lens
  assert.match(html, /data-sprint-id="3"/);
});

test('the phone layout is three lines and a 32 px bar — no chart, no overflow', () => {
  const html = forecastCard(forecastPayload());
  const phone = /<div class="forecast-phone">([\s\S]*?)<\/div>\s*<\/section>/.exec(html);
  assert.ok(phone, 'the phone block is present');
  const block = phone[1];
  // three lines: interval, by · P50, and the at-risk disclosure
  assert.match(block, /class="forecast-headline">22–30 of 30</);
  assert.match(block, /by 27 Dec · P50 27/);
  assert.match(block, /2 at risk →/);
  assert.match(block, /data-forecast-risk-toggle/);
  // the bar and its three numbers, and nothing else
  assert.match(block, /data-forecast-bar/);
  assert.match(block, /class="forecast-ends phone"/);
  // no chart / axis / scroll container
  assert.doesNotMatch(block, /<svg|<canvas|forecast-axis|table-wrap/);
  assert.doesNotMatch(block, /forecast-method/);
  // the rows are a disclosure, hidden until tapped
  assert.match(block, /data-forecast-phone-rows hidden/);
  const css = read('style.css');
  assert.match(css, /@media \(max-width: 640px\)[\s\S]*?\.forecast-desktop \{ display: none; \}/);
  assert.match(css, /\.forecast-phone \.forecast-bar \{ height: 32px; max-width: 100%; \}/);
});

test('scope added after the start is stated, never silently adjusted', () => {
  const payload = forecastPayload({ scope_added_after_start: 3 });
  const html = forecastCard(payload);
  assert.match(html, /this forecasts the scope as of today/);
  assert.match(html, /3 stories were added after\s+the sprint started/);
  // the interval itself is unchanged: the caveat is the disclosure
  assert.match(html, /22–30 of 30/);
});

test('unfinished runs inside the horizon are stated, never hidden', () => {
  const payload = forecastPayload();
  payload.forecast.when = {
    ...payload.forecast.when, p85_weeks: null, p85_date: null,
    unfinished_share: 0.04,
  };
  const html = forecastCard(payload);
  assert.match(html, /4 % of runs do not land inside 26 weeks/);
});

test('the interval label is an en dash and never a single value', () => {
  assert.equal(intervalLabel({ p15: 22, p85: 30 }), '22–30');
  assert.equal(intervalLabel(null), '');
  const html = forecastCard(forecastPayload());
  assert.doesNotMatch(html, /class="forecast-headline">27 of 30/);
});

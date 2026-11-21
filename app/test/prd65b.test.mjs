/* prd/65 B — N-02: the forecast card's degenerate interval and its export.
 *
 * Two bugs from docs/QA-v012-final.md, both in `app/forecast.js`:
 *
 *   1. a capped target can make P15 = P50 = P85 (`30/30/30`); the three end
 *      labels all sat at `left: 100 %` and rendered `"3030"` under the bar.
 *      Coinciding positions now print the value once (at the right edge); a
 *      normal 20/26/30 still prints three distinct labels.
 *   2. the card had no `Copy as Markdown`. `forecastMarkdown()` formats the
 *      payload the card already rendered — interval, by-date, P50, p(reach),
 *      n/mean/sd/CV, the at-risk ceiling and any warning — and keeps
 *      `?tenant=` in every link. It never fetches and never re-simulates.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { forecastBar, forecastCard, forecastMarkdown } from '../forecast.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const BASE = 'https://qa.example/?tenant=del-combined';

/* ------------------------------------------------------------------ fixtures */

function forecastPayload(overrides = {}) {
  const base = {
    ok: true,
    available: true,
    as_of: '2026-11-18T00:10:44Z',
    method: 'Method: 10,000 resamples of 13 weeks of stories delivered (mean 6.5, sd 2.6, CV 0.41). Not points — points and logged hours do not track time here (r ≈ −0.05).',
    caveat: 'Both numbers are the 85 % edge: "by" uses P85 because a date is a promise; "how many" uses P15 for the same reason.',
    throughput: {
      basis: 'stories delivered / week', n: 13, mean: 6.46, sd: 2.65, cv: 0.41,
      values: [2, 3, 7, 8, 6, 7, 5, 7, 10, 4, 8, 5, 12],
    },
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
      when: {
        p50_weeks: 5, p85_weeks: 6, p95_weeks: 7, p50_date: '2027-01-03',
        p85_date: '2027-01-10', unfinished_share: 0, start: '2026-11-30',
      },
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
      doubtful: 1,
      delivered_if_the_blocked_items_hold: 29,
    },
    delegate: null,
    note: null,
  };
  return { ...base, ...overrides };
}

/** A capped target: every quantile is `n`, the payload QA saw on sprint 3. */
function cappedPayload(quantiles = { p15: 40, p50: 42, p85: 45 }) {
  const source = forecastPayload();
  return forecastPayload({
    forecast: { ...source.forecast, uncapped: quantiles },
  });
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

const SPARSE = 'Not enough history to forecast: 3 weeks observed, 8 needed.';

/** The `.forecast-ends` spans: one per rendered label. */
function endSpans(html) {
  const block = /<div class="forecast-ends[^"]*">([\s\S]*?)<\/div>/.exec(html);
  assert.ok(block, 'the ends block is present');
  return {
    markup: block[1],
    spans: [...block[1].matchAll(/<span style="left:([\d.]+)%">([^<]*)<\/span>/g)]
      .map((match) => ({ pos: Number(match[1]), text: match[2] })),
  };
}

/* --------------------------------------------------------- the label overlap */

test('a 30/30/30 interval prints one end label, never three at the right edge', () => {
  const html = forecastCard(cappedPayload());
  const ends = endSpans(html);
  assert.equal(ends.spans.length, 1);
  assert.deepEqual(ends.spans[0], { pos: 100, text: '30' });
  // the observed bug: three `30` text nodes side by side render as `3030`
  assert.equal((ends.markup.match(/>30</g) || []).length, 1);
  assert.doesNotMatch(ends.markup, /3030/);
  // the encoding did not change — the headline is still the (degenerate) interval
  assert.match(html, /class="forecast-headline">30–30 of 30/);
  assert.match(html, /aria-label="interval 30 to 30, P50 30, target 30"/);
});

test('the phone bar collapses the same way (one 30, not two)', () => {
  const html = forecastCard(cappedPayload());
  const phone = /<div class="forecast-phone">([\s\S]*?)<\/div>\s*<\/section>/.exec(html);
  assert.ok(phone, 'the phone block is present');
  const ends = endSpans(phone[1]);
  assert.equal(ends.spans.length, 1);
  assert.equal(ends.spans[0].text, '30');
  assert.equal((ends.markup.match(/>30</g) || []).length, 1);
});

test('a normal 20/26/30 keeps three distinct labels', () => {
  const html = forecastCard(cappedPayload({ p15: 20, p50: 26, p85: 30 }));
  const ends = endSpans(html);
  assert.deepEqual(ends.spans, [
    { pos: 0, text: '20' },
    { pos: 60, text: '26' },
    { pos: 100, text: '30' },
  ]);
  // ...and the bar is still proportional (20/26/30 → 0 %/60 %/100 %)
  assert.match(html, /data-p50-pos="60\.00"/);
});

test('two coincident quantiles collapse, two distinct ones stay', () => {
  // P50 = P85 = 30 in a 22–30 interval: the middle label is the same label
  const view = { n: 30, p15: 22, p50: 30, p85: 30, p_reach: 0.29 };
  const ends = endSpans(forecastBar(view, 30));
  assert.deepEqual(ends.spans, [
    { pos: 0, text: '22' },
    { pos: 100, text: '30' },
  ]);
  // labels more than 2 % of the bar apart never merge (nearest case: 29 and 30)
  const near = endSpans(forecastBar({ n: 30, p15: 29, p50: 29, p85: 30, p_reach: 0.5 }, 30));
  assert.deepEqual(near.spans.map((span) => span.text), ['29', '30']);
});

/* ------------------------------------------------------------- the export */

test('forecastMarkdown carries the interval, the by-date, p(reach) and the sample', () => {
  const markdown = forecastMarkdown(forecastPayload(), { base: BASE });
  assert.match(markdown, /^# Forecast — 22–30 of 30 · by 27 Dec/m);
  assert.match(markdown, /- interval: \*\*22–30\*\* of 30/);
  assert.match(markdown, /- by: 27 Dec \(2026-12-27\)/);
  assert.match(markdown, /- P50: 27/);
  assert.match(markdown, /- p\(reach\): 29 % chance all 30/);
  assert.match(markdown, /throughput \(stories delivered \/ week\): n = 13 weeks/);
  assert.match(markdown, /mean 6\.5/);
  assert.match(markdown, /sd 2\.6/);
  assert.match(markdown, /CV 0\.41/);
  assert.match(markdown, /- P85 date: 85 % by 10 Jan \(2027-01-10\)/);
  assert.match(markdown, /A forecast, not a measurement/);
  assert.match(markdown, /seed 1234567/);
});

test('the at-risk ceiling and the tenant are in the export, links included', () => {
  const markdown = forecastMarkdown(forecastPayload(), { base: BASE });
  assert.match(markdown, /## What the model can't see/);
  assert.match(markdown, /2 at risk · 30 left/);
  assert.match(markdown, /delivered if the blocked items hold: \*\*29\*\*/);
  // the tenant travels in a link: a copied block opened on the default dataset
  // would be a different store with different numbers for the same key.
  assert.match(markdown, /\[del-combined\]\(https:\/\/qa\.example\/\?tenant=del-combined\)/);
  assert.match(markdown,
    /\[DEL-176\]\(https:\/\/qa\.example\/\?tenant=del-combined#ticket\/DEL-176\) #60 stuck on FrMo · 18 d 0 h/);
});

test('the export re-reads the selected target, never a re-run', () => {
  const markdown = forecastMarkdown(forecastPayload(), { target: 26, base: BASE });
  assert.match(markdown, /22–26 of 26/);
  assert.match(markdown, /78 % chance all 26/);
  // no network and no client-side simulation anywhere in the module
  assert.doesNotMatch(read('forecast.js'), /\bfetch\(|XMLHttpRequest/);
});

test('a warning is stated in the export, not hidden behind the interval', () => {
  const markdown = forecastMarkdown(forecastPayload(), { target: 33, base: BASE });
  assert.match(markdown, /\*\*Warning:\*\* even the optimistic edge \(P85 = 32\) misses the target of 33/);
  // the interval is still the truth, not narrowed to make the warning go away
  assert.match(markdown, /22–32 of 33/);
});

test('a refused card exports the refusal and adds no number of its own', () => {
  const payload = refusalPayload('sparse', SPARSE);
  const markdown = forecastMarkdown(payload);
  assert.match(markdown, /^# Forecast — refused/m);
  assert.match(markdown, new RegExp(SPARSE.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  // nothing the export adds may be a number: strip the verbatim sentence and
  // the rest must be digit-free (no zero dressed up as an interval).
  assert.doesNotMatch(markdown.replace(SPARSE, ''), /\d/);
  assert.doesNotMatch(markdown, /P50|chance all|delivered if the blocked items hold/);
  assert.doesNotMatch(markdown, /[0-9]–[0-9]/);
  // the refused card still offers the chip (the refusal is the honest export)
  assert.match(forecastCard(payload), /data-forecast-copy="fc\d+"/);
});

test('a delegated small set exports the names, not a fake interval', () => {
  const payload = {
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
  };
  const markdown = forecastMarkdown(payload, { base: BASE });
  assert.match(markdown, /^# Forecast — not forecastable/m);
  assert.match(markdown, /a 2-item set is a risk question, not a throughput question/);
  assert.match(markdown,
    /\[DEL-176\]\(https:\/\/qa\.example\/\?tenant=del-combined#ticket\/DEL-176\) #60 stuck on FrMo · 18 d 0 h/);
  assert.doesNotMatch(markdown, /chance all|p\(reach\)/);
});

/* --------------------------------------------------------------- the chip */

test('the card carries the shared copy chip and this module owns its handler', () => {
  const html = forecastCard(forecastPayload());
  const chips = html.match(/<button type="button" class="chip" data-forecast-copy="fc\d+"/g) || [];
  assert.equal(chips.length, 1);
  assert.match(html, /title="copy the interval, the by-date, p\(reach\), the at-risk ceiling/);
  assert.match(html, />Copy as Markdown</);
  // `main.js` does not route the forecast export; the handler lives here so the
  // card can copy without touching the page's click router.
  const source = read('forecast.js');
  assert.match(source, /\[data-forecast-copy\]/);
  assert.match(source, /FORECAST_COPY\.get\(chip\.dataset\.forecastCopy\)/);
  assert.match(source, /export function forecastMarkdown\(/);
});

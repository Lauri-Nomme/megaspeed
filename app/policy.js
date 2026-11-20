/* Policy lab — "What should we change first?" (prd/23, prd/09 View 4).
 *
 * Two rules shape this page, and they are the whole point of the feature:
 *
 * 1. **Nothing is shown until the baseline validates.** The model is only a
 *    model; if its baseline cannot reproduce the historical cycle-time median
 *    on this store, the page says so with the mismatch and refuses to run a
 *    scenario. A confident-looking wrong number is worse than no number.
 * 2. **Every predicted number is an estimate and is marked `≈`.** The measured
 *    historical number never is. The provenance line states the calibration
 *    window, the PR count and the replication count, so a reader can judge how
 *    much weight the prediction deserves.
 *
 * The pure helpers here are unit-tested in `app/test/policy.test.mjs`; the page
 * wiring (fetch, state, events) lives in `main.js`, as with every other view.
 */

import { esc, fmtHours } from './format.js';

/** A predicted value: always `≈`, never a bare number (prd/10 §3.4). */
export function predHours(hours) {
  if (hours == null || !isFinite(hours)) return '≈–';
  return `≈${fmtHours(hours)}`;
}

export function predPercent(share) {
  if (share == null || !isFinite(share)) return '≈–';
  return `≈${Math.round(share * 100)}%`;
}

export function predRate(perDay) {
  if (perDay == null || !isFinite(perDay)) return '≈–';
  return `≈${Number(perDay).toFixed(2)} /d`;
}

/** The knobs the model accepts, in the order the page shows them. */
export const KNOBS = [
  {
    key: 'wip_limit', label: 'WIP limit / author', kind: 'select',
    options: [['', 'off (today)'], ['1', '1'], ['2', '2'], ['3', '3']],
    note: 'a pull system: excess PRs wait in the backlog (lead time grows, cycle shrinks)',
  },
  {
    key: 'sentinels', label: 'sentinels', kind: 'select',
    options: [['1', '1 (today)'], ['2', '2'], ['3', '3']],
    note: 'parallel mandatory-approval slots (a rotation)',
  },
  {
    key: 'review_sla_hours', label: 'review SLA', kind: 'select',
    options: [['', 'off'], ['2', '2 h'], ['4', '4 h'], ['8', '8 h'], ['24', '24 h (3 d)']],
    note: 'an overdue review is escalated to any free reviewer instead of waiting',
  },
  {
    key: 'size_cap_commits', label: 'PR size cap', kind: 'select',
    options: [['', 'off'], ['3', '3 commits'], ['5', '5 commits'], ['8', '8 commits']],
    note: 'a bigger PR is split into that many-commit PRs (more PRs, less each)',
  },
  {
    key: 'stack_depth_cap', label: 'stack depth cap', kind: 'select',
    options: [['', 'off'], ['1', '1'], ['2', '2']],
    note: 'deeper stacks are flattened into independent PRs (inert without ref digests)',
  },
  {
    key: 'review_first', label: 'review before new work', kind: 'toggle',
    note: 'reviews preempt the reviewer’s own authoring',
  },
  {
    // prd/42 M3: the two options a lead names that the lab could not test.
    key: 'refine_before_start', label: 'refine before start', kind: 'toggle',
    note: 'spend the measured created → ready refinement before the first review; one rework round is caught before it happens',
  },
  {
    key: 'review_round_cap', label: 'review-round cap', kind: 'select',
    options: [['', 'off (today)'], ['1', '1 round'], ['2', '2 rounds'], ['3', '3 rounds']],
    note: 'stop the loop at the cap: the owner and reviewer agree the remainder in one pass',
  },
];

/** A named policy the page can apply in one click. */
export const PRESETS = [
  { id: 'sentinel2', label: 'add a sentinel', scenario: { sentinels: 2 } },
  { id: 'sla4', label: 'review SLA 4 h', scenario: { review_sla_hours: 4 } },
  { id: 'wip1', label: 'WIP limit 1', scenario: { wip_limit: 1 } },
  { id: 'reviewfirst', label: 'review first', scenario: { review_first: true } },
  { id: 'size3', label: 'cap PRs at 3 commits', scenario: { size_cap_commits: 3 } },
  { id: 'refine', label: 'refine before start', scenario: { refine_before_start: true } },
  { id: 'roundcap2', label: 'cap review at 2 rounds', scenario: { review_round_cap: 2 } },
];

/** The scenario object a page state holds, from the knob defaults. */
export function defaultScenario() {
  return { name: 'scenario', wip_limit: null, sentinels: 1, review_sla_hours: null,
    size_cap_commits: null, stack_depth_cap: null, review_first: false,
    refine_before_start: false, review_round_cap: null };
}

/** Read the controls back into a scenario (numbers, not strings). */
export function readScenario(root) {
  const scenario = defaultScenario();
  const value = (key) => {
    const node = root.querySelector(`[data-knob="${key}"]`);
    if (!node) return '';
    return node.type === 'checkbox' ? node.checked : node.value;
  };
  const number = (key) => {
    const raw = value(key);
    return raw === '' || raw == null ? null : Number(raw);
  };
  scenario.wip_limit = number('wip_limit');
  scenario.sentinels = number('sentinels') || 1;
  scenario.review_sla_hours = number('review_sla_hours');
  scenario.size_cap_commits = number('size_cap_commits');
  scenario.stack_depth_cap = number('stack_depth_cap');
  scenario.review_first = Boolean(value('review_first'));
  scenario.refine_before_start = Boolean(value('refine_before_start'));
  scenario.review_round_cap = number('review_round_cap');
  return scenario;
}

/** Is this scenario the historical process? Then there is nothing to compare. */
export function isBaselineScenario(scenario) {
  return !scenario
    || (!scenario.wip_limit && (scenario.sentinels ?? 1) === 1
      && !scenario.review_sla_hours && !scenario.size_cap_commits
      && !scenario.stack_depth_cap && !scenario.review_first
      && !scenario.refine_before_start && !scenario.review_round_cap);
}

/** The query string for `/api/simulate`, scenario as compact JSON. */
export function scenarioQuery(scenario, { seed } = {}) {
  const params = new URLSearchParams();
  if (scenario && !isBaselineScenario(scenario)) {
    params.set('scenario', JSON.stringify(scenario));
  }
  if (seed != null) params.set('seed', String(seed));
  return params.toString();
}

export function policyConfigMarkup(scenario) {
  const current = scenario || defaultScenario();
  const rows = KNOBS.map((knob) => {
    const value = current[knob.key];
    // prd/37 §5 (a11y): every control has a real <label for>, not just a span
    // beside it, and its note is wired through aria-describedby.
    const id = `policy-knob-${knob.key}`;
    const noteId = `${id}-note`;
    let control = '';
    if (knob.kind === 'toggle') {
      control = `<input type="checkbox" id="${id}" data-knob="${knob.key}"` +
        ` aria-describedby="${noteId}"${value ? ' checked' : ''}>`;
    } else {
      const options = knob.options.map(([optionValue, label]) => {
        const chosen = String(value ?? '') === optionValue ? ' selected' : '';
        return `<option value="${optionValue}"${chosen}>${esc(label)}</option>`;
      }).join('');
      control = `<select id="${id}" data-knob="${knob.key}" aria-describedby="${noteId}">${options}</select>`;
    }
    return `<div class="policy-knob">
      <label class="policy-knob-k" for="${id}">${esc(knob.label)}</label>
      ${control}
      <span class="policy-knob-note" id="${noteId}">${esc(knob.note)}</span>
    </div>`;
  }).join('');
  const presets = PRESETS.map((preset) =>
    `<button type="button" class="chip" data-preset="${esc(preset.id)}">${esc(preset.label)}</button>`,
  ).join('');
  return `<div class="policy-knobs">${rows}</div>
    <div class="policy-actions">
      <button type="button" class="policy-run" id="policy-run">Run scenario</button>
      <button type="button" class="chip" id="policy-reset">Reset to baseline</button>
    </div>
    <div class="policy-presets"><span class="legend-label">presets</span>${presets}</div>
    <p class="policy-combine" id="policy-combine">A scenario can combine knobs:
      sentinel + review SLA is one run, and the model composes them. One knob at
      a time understates the effect — the queue moves to whichever constraint
      is left.</p>`;
}

/** The gate: the one sentence that decides whether anything else is credible. */
export function validationMarkup(data) {
  if (!data) return '';
  const gate = data.validation;
  if (!data.available) {
    return `<div class="policy-gate blocked">
      <span class="pill overloaded">not calibrated</span>
      <span>${esc(data.note || 'the store has nothing to calibrate a model from')}</span></div>`;
  }
  if (!gate) return '';
  const historical = gate.historical_median_hours;
  const predicted = gate.predicted_median_hours;
  if (gate.status === 'validated') {
    return `<div class="policy-gate ok">
      <span class="pill healthy">baseline validated</span>
      <span><b>${predHours(predicted)}</b> predicted vs <b>${fmtHours(historical)}</b>
        measured cycle-time median — <b>${signed(gate.percent_off)}</b>
        (tolerance ±${Math.round((gate.tolerance ?? 0.2) * 100)} %)</span></div>`;
  }
  return `<div class="policy-gate blocked">
    <span class="pill overloaded">baseline failed</span>
    <span>the model predicts <b>${predHours(predicted)}</b> but the store measured
      <b>${fmtHours(historical)}</b> (<b>${signed(gate.percent_off)}</b>, tolerance
      ±${Math.round((gate.tolerance ?? 0.2) * 100)} %) — <b>scenario numbers are withheld</b>,
      because a model that cannot reproduce the last quarter cannot predict the next one.</span></div>`;
}

function signed(percent) {
  if (percent == null || !isFinite(percent)) return '–';
  const value = Number(percent);
  return `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(1)} %`;
}

function deltaCell(entry) {
  if (!entry || entry.percent == null) return '<td class="c-delta muted" data-label="Δ">–</td>';
  const cls = entry.better === true ? 'good' : entry.better === false ? 'bad' : 'muted';
  const arrow = entry.direction === 'down' ? '▾' : entry.direction === 'up' ? '▴' : '·';
  return `<td class="c-delta ${cls}" data-label="Δ">${arrow} ${signed(entry.percent)}</td>`;
}

/**
 * prd/37 §5: every scenario row gets a verdict line, not just a colour — and
 * the lead-time blow-up is a sentence, not a percentage the reader must decode.
 */
export function verdictCell(entry) {
  if (!entry || !entry.verdict) return '<td class="c-verdict muted" data-label="verdict">–</td>';
  return `<td class="c-verdict ${entry.verdict_tone || 'muted'}" data-label="verdict">${esc(entry.verdict)}</td>`;
}

function row(label, baseline, scenario, delta, note = '') {
  return `<tr>
    <td class="c-metric" data-label="">${esc(label)}${note ? `<span class="policy-row-note">${esc(note)}</span>` : ''}</td>
    <td class="c-base" data-label="baseline">${baseline}</td>
    <td class="c-scen" data-label="scenario">${scenario}</td>
    ${deltaCell(delta)}
    ${verdictCell(delta)}
  </tr>`;
}

/** The A/B table: predicted distributions side by side, deltas last. */
export function abMarkup(data) {
  if (!data || !data.baseline || !data.baseline.ok) return '';
  const base = data.baseline;
  const scenario = data.scenario;
  const compare = data.compare;
  const left = base.predicted;
  const right = scenario ? scenario.predicted : null;
  const head = `<tr>
    <th class="c-metric">metric</th>
    <th class="c-base">baseline</th>
    <th class="c-scen">${scenario ? esc(scenario.scenario.name || 'scenario') : 'scenario'}</th>
    <th class="c-delta">Δ</th>
    <th class="c-verdict">verdict</th>
  </tr>`;
  const rows = [
    row('cycle time · median', predHours(left.cycle_hours.median),
      right ? predHours(right.cycle_hours.median) : '—',
      compare && compare.cycle_median_hours, 'open → merge'),
    row('cycle time · p90', predHours(left.cycle_hours.p90),
      right ? predHours(right.cycle_hours.p90) : '—',
      compare && compare.cycle_p90_hours),
    row('flow efficiency', predPercent(left.flow_efficiency.aggregate),
      right ? predPercent(right.flow_efficiency.aggregate) : '—',
      compare && compare.flow_efficiency, 'active ÷ elapsed'),
    // prd/37 §5: delivered *work* is the throughput that compares policies;
    // the PR count follows as packaging, never as the headline.
    row(`delivered work · ${left.work_unit || 'commits'} / d`, predRate(left.throughput_work_per_day),
      right ? predRate(right.throughput_work_per_day) : '—',
      compare && compare.throughput_work_per_day,
      'merged work units ÷ business day'),
    row('PR count · / d', predRate(left.throughput_per_day),
      right ? predRate(right.throughput_per_day) : '—',
      compare && compare.throughput_per_day, 'packaging, not more work'),
    row('lead time · median', predHours(left.lead_hours.median),
      right ? predHours(right.lead_hours.median) : '—',
      compare && compare.lead_hours, 'arrival → merge'),
  ].join('');
  const bottleneck = `<tr>
    <td class="c-metric" data-label="">bottleneck</td>
    <td class="c-base" data-label="baseline">${esc((base.bottleneck && base.bottleneck.person) || '–')}
      ${base.bottleneck ? `<span class="policy-row-note">${predPercent(base.bottleneck.utilisation)} busy</span>` : ''}</td>
    <td class="c-scen" data-label="scenario">${scenario
      ? `${esc((scenario.bottleneck && scenario.bottleneck.person) || '–')}
        ${scenario.bottleneck ? `<span class="policy-row-note">${predPercent(scenario.bottleneck.utilisation)} busy</span>` : ''}`
      : '—'}</td>
    <td class="c-delta muted" data-label="Δ">–</td>
    <td class="c-verdict muted" data-label="verdict">–</td></tr>`;
  const noise = compare && compare.within_noise
    ? `<p class="policy-note">The cycle-time change is <b>smaller than the run-to-run spread</b>
        of the model (${predHours(left.spread && left.spread.median_min)} –
        ${predHours(left.spread && left.spread.median_max)} across
        ${esc(left.spread && left.spread.replications)} replications), so read it as
        “no clear effect”, not as a win.</p>`
    : '';
  const verdict = compare && compare.verdict
    ? `<p class="policy-verdict ${esc(compare.verdict_tone || 'muted')}" id="policy-verdict">
        <b>verdict:</b> ${esc(compare.verdict)}</p>`
    : '';
  return `<table class="table policy-table policy-ab"><thead>${head}</thead>
    <tbody>${rows}${bottleneck}</tbody></table>${verdict}${noise}`;
}

/** Per-server utilisation: who is *busy* (calendar share), and where the queue is.
 *
 * concepts.md §2/§6: utilisation (ρ, calendar share) is a different metric from
 * the queue view's "overloaded" (queue wait). The label says "busy" and the
 * table note says which lens this is; neither number is ever called "load".
 */
export function resourcesMarkup(result) {
  if (!result || !result.resources) return '';
  const rows = result.resources.map((resource) => {
    // the CSS class carries the colour; the text carries the distinct metric
    const tone = resource.utilisation >= 0.85 ? 'overloaded'
      : resource.utilisation >= 0.5 ? 'slow' : 'healthy';
    // prd/46 §6: ρ is the share *busy*. "≈27 % idle" read as 73 % busy; the
    // chip now states the same number as the share it is.
    const label = 'busy';
    return `<tr>
      <td class="c-who" data-label="">${esc(resource.person)}</td>
      <td class="c-role" data-label="role">${esc(resource.role)}</td>
      <td data-label="utilisation"><span class="pill ${tone}" title="calendar share (ρ): the share of this person's calendar spent busy — not the complement, and not queue wait">${predPercent(resource.utilisation)} ${label}</span></td>
      <td class="c-dur" data-label="mean wait">${predHours(resource.mean_wait_hours)}</td>
      <td class="c-dur" data-label="peak queue">≈${resource.peak_queue}</td>
      <td class="c-dur" data-label="requests">≈${resource.requests}</td>
    </tr>`;
  }).join('');
  const warnings = (result.warnings || []).map((warning) =>
    `<p class="policy-warning">${esc(warning)}</p>`).join('');
  return `<table class="table compact policy-table"><thead><tr>
      <th class="c-who">server</th><th class="c-role">role</th>
      <th title="calendar share (ρ), not queue wait">busy (ρ)</th><th class="c-dur">mean wait</th>
      <th class="c-dur">peak queue</th><th class="c-dur">requests</th>
    </tr></thead><tbody>${rows}</tbody></table>
    <p class="panel-note">busy = calendar share (ρ); the queue view's “overloaded” is queue wait — different metrics.</p>${warnings}`;
}

/** Calibrated facts: what the model was fitted from, in plain numbers. */
export function calibrationMarkup(calibration) {
  if (!calibration || !calibration.ok) return '';
  const window = calibration.window || {};
  const arrival = calibration.arrival || {};
  const sentinel = calibration.sentinel || {};
  const rework = calibration.rework || {};
  const size = calibration.pr_size || {};
  const stack = calibration.stack_depth || {};
  const refinement = calibration.refinement || {};
  const facts = [
    ['PRs opened', `${calibration.prs.n} (${calibration.prs.merged} merged)`],
    ['arrival', `${predRate(arrival.per_business_day)} · ≈${arrival.mean_interarrival_hours} h apart`],
    ['review response', `≈${calibration.review_service.pooled.median} h median`],
    ['rework', `${predPercent(rework.probability)} of PRs · ${predPercent(rework.push_class.content)} content`],
    ['sentinel gate', `≈${sentinel.observed.mean_hours} h observed · ${predPercent(sentinel.utilisation)} busy`],    ['PR size', `≈${size.commits.median} commits median`],
    // prd/42 M3: the refinement knob prices a measured segment, not a guess.
    ['refinement', refinement.hours == null ? 'not measurable (no Jira layer)' : `≈${refinement.hours} h created → ready`],
    ['stack depth', stack.available ? `${predPercent(0)} measured` : 'not measurable (no ref digests)'],
    ['window', `${window.business_days} business days · ${window.business_hours} h`],
  ];
  return `<dl class="facts policy-facts">${facts.map(([key, value]) =>
    `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>
    <p class="panel-note">the sentinel gate here is the model's fitted <b>mean</b> server time over review-ball episodes — a different metric from the Queue's sentinel wait (median, last peer answer → first response).</p>`;
}

/** The provenance line every predicted number rides on (prd/23). */
export function provenanceLine(data) {
  if (!data) return '';
  const calibration = data.calibration || {};
  const baseline = data.baseline || {};
  const spread = (baseline.predicted && baseline.predicted.spread) || {};
  const mode = data.hours_mode === 'business' ? 'business hours' : 'wall-clock hours';
  const parts = [
    data.provenance || 'calibration unavailable',
    `${mode}`,
    data.calendar_id ? `calendar ${data.calendar_id}` : null,
    spread.replications ? `${spread.replications} replications` : null,
    data.seed != null ? `seed ${data.seed}` : null,
    'every predicted number is an estimate (≈)',
  ].filter(Boolean);
  return parts.map((part) => esc(part)).join(' · ');
}

/** The page body: config, gate, A/B, bottleneck. */
export function policyMarkup(data, scenario) {
  const gate = validationMarkup(data);
  const config = `<div class="policy-panel policy-config">
      <h2 class="policy-h">Scenario</h2>
      ${policyConfigMarkup(scenario)}
    </div>`;
  if (!data) {
    return `<div class="policy-wrap">${config}
      <div class="policy-panel"><p class="empty-row">Loading the calibrated model…</p></div></div>`;
  }
  // The gate decides what may be shown. When the baseline does not reproduce
  // the store, the *model's* numbers (baseline or scenario) are not evidence —
  // only the measured calibration facts are.
  const trusted = !data.validation || data.validation.ok;
  const result = data.scenario || data.baseline;
  const ab = data.scenario
    ? abMarkup(data)
    : `<p class="empty-row">Baseline only. Pick a knob and run a scenario to compare it
        against today’s process.</p>`;
  const blocked = data.blocked
    ? `<p class="policy-warning">${esc(data.blocked)}</p>` : '';
  const modelled = trusted
    ? `<h2 class="policy-h">A / B · predicted cycle time</h2>
      ${ab}
      <h2 class="policy-h">Who is busy · calendar share (ρ)</h2>
      ${resourcesMarkup(result)}`
    : `<p class="empty-row">The model did not validate, so its own numbers — baseline
        and scenario alike — are withheld. The calibrated facts below are measured
        from the store and still hold.</p>`;
  const body = `<div class="policy-panel policy-result">
      ${blocked}${modelled}
      <h2 class="policy-h">Calibrated from</h2>
      ${calibrationMarkup(data.calibration)}
    </div>`;
  return `<div class="policy-wrap">${gate}${config}${body}
    <p class="policy-provenance" id="policy-provenance">${provenanceLine(data)}</p>
    <p class="policy-note">${esc((data.calibration && data.calibration.queueing && data.calibration.queueing.note) || '')}</p>
  </div>`;
}

/** Copy-as-markdown: the A/B with its provenance, for a ticket or a retro. */
export function policyMarkdown(data) {
  if (!data || !data.baseline || !data.baseline.ok) return '';
  const base = data.baseline.predicted;
  const scenario = data.scenario ? data.scenario.predicted : null;
  const compare = data.compare;
  const gate = data.validation || {};
  const lines = [
    '# Policy lab — what should we change first?',
    '',
    `_${data.provenance} · ${data.hours_mode === 'business' ? 'business hours' : 'wall-clock hours'} · ` +
      `${data.calendar_id ? `calendar ${data.calendar_id} · ` : ''}` +
      `baseline ${gate.status || 'unknown'} (${signed(gate.percent_off)} vs measured)_`,
    '',
    '| metric | baseline | scenario | Δ | verdict |',
    '| --- | --- | --- | --- | --- |',
    `| cycle median | ${predHours(base.cycle_hours.median)} | ${scenario ? predHours(scenario.cycle_hours.median) : '—'} | ${compare ? signed(compare.cycle_median_hours.percent) : '—'} | ${(compare && compare.cycle_median_hours.verdict) || '—'} |`,
    `| cycle p90 | ${predHours(base.cycle_hours.p90)} | ${scenario ? predHours(scenario.cycle_hours.p90) : '—'} | ${compare ? signed(compare.cycle_p90_hours.percent) : '—'} | ${(compare && compare.cycle_p90_hours.verdict) || '—'} |`,
    `| flow efficiency | ${predPercent(base.flow_efficiency.aggregate)} | ${scenario ? predPercent(scenario.flow_efficiency.aggregate) : '—'} | ${compare ? signed(compare.flow_efficiency.percent) : '—'} | ${(compare && compare.flow_efficiency.verdict) || '—'} |`,
    `| delivered work · ${base.work_unit || 'commits'} / d | ${predRate(base.throughput_work_per_day)} | ${scenario ? predRate(scenario.throughput_work_per_day) : '—'} | ${compare ? signed(compare.throughput_work_per_day.percent) : '—'} | ${(compare && compare.throughput_work_per_day.verdict) || '—'} |`,
    `| PR count · / d | ${predRate(base.throughput_per_day)} | ${scenario ? predRate(scenario.throughput_per_day) : '—'} | ${compare ? signed(compare.throughput_per_day.percent) : '—'} | packaging, not more work |`,
    `| lead time | ${predHours(base.lead_hours.median)} | ${scenario ? predHours(scenario.lead_hours.median) : '—'} | ${compare ? signed(compare.lead_hours.percent) : '—'} | ${(compare && compare.lead_hours.verdict) || '—'} |`,
    `| bottleneck | ${(data.baseline.bottleneck && data.baseline.bottleneck.person) || '–'} | ${scenario && data.scenario.bottleneck ? data.scenario.bottleneck.person : '—'} | | |`,
    '',
    compare && compare.verdict ? `**verdict:** ${compare.verdict}` : '',
    `_Every predicted number is an estimate (≈); measured numbers are not. ${compare && compare.within_noise ? 'The delta is within the model\'s run-to-run spread.' : ''}_`,
  ];
  if (data.blocked) lines.splice(2, 0, `> ${data.blocked}`, '');
  return lines.join('\n');
}

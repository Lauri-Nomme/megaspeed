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
    options: [['', 'off'], ['2', '2 h'], ['4', '4 h'], ['8', '8 h'], ['24', '1 d']],
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
];

/** A named policy the page can apply in one click. */
export const PRESETS = [
  { id: 'sentinel2', label: 'add a sentinel', scenario: { sentinels: 2 } },
  { id: 'sla4', label: 'review SLA 4 h', scenario: { review_sla_hours: 4 } },
  { id: 'wip1', label: 'WIP limit 1', scenario: { wip_limit: 1 } },
  { id: 'reviewfirst', label: 'review first', scenario: { review_first: true } },
  { id: 'size3', label: 'cap PRs at 3 commits', scenario: { size_cap_commits: 3 } },
];

/** The scenario object a page state holds, from the knob defaults. */
export function defaultScenario() {
  return { name: 'scenario', wip_limit: null, sentinels: 1, review_sla_hours: null,
    size_cap_commits: null, stack_depth_cap: null, review_first: false };
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
  return scenario;
}

/** Is this scenario the historical process? Then there is nothing to compare. */
export function isBaselineScenario(scenario) {
  return !scenario
    || (!scenario.wip_limit && (scenario.sentinels ?? 1) === 1
      && !scenario.review_sla_hours && !scenario.size_cap_commits
      && !scenario.stack_depth_cap && !scenario.review_first);
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
    let control = '';
    if (knob.kind === 'toggle') {
      control = `<input type="checkbox" data-knob="${knob.key}"${value ? ' checked' : ''}>`;
    } else {
      const options = knob.options.map(([optionValue, label]) => {
        const chosen = String(value ?? '') === optionValue ? ' selected' : '';
        return `<option value="${optionValue}"${chosen}>${esc(label)}</option>`;
      }).join('');
      control = `<select data-knob="${knob.key}">${options}</select>`;
    }
    return `<label class="policy-knob">
      <span class="policy-knob-k">${esc(knob.label)}</span>
      ${control}
      <span class="policy-knob-note">${esc(knob.note)}</span>
    </label>`;
  }).join('');
  const presets = PRESETS.map((preset) =>
    `<button type="button" class="chip" data-preset="${esc(preset.id)}">${esc(preset.label)}</button>`,
  ).join('');
  return `<div class="policy-knobs">${rows}</div>
    <div class="policy-actions">
      <button type="button" class="policy-run" id="policy-run">Run scenario</button>
      <button type="button" class="chip" id="policy-reset">Reset to baseline</button>
    </div>
    <div class="policy-presets"><span class="legend-label">presets</span>${presets}</div>`;
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
  if (!entry || entry.percent == null) return '<td class="c-delta muted">–</td>';
  const cls = entry.better === true ? 'good' : entry.better === false ? 'bad' : 'muted';
  const arrow = entry.direction === 'down' ? '▾' : entry.direction === 'up' ? '▴' : '·';
  return `<td class="c-delta ${cls}">${arrow} ${signed(entry.percent)}</td>`;
}

function row(label, baseline, scenario, delta, note = '') {
  return `<tr>
    <td class="c-metric">${esc(label)}${note ? `<span class="policy-row-note">${esc(note)}</span>` : ''}</td>
    <td class="c-base">${baseline}</td>
    <td class="c-scen">${scenario}</td>
    ${deltaCell(delta)}
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
    row('throughput', predRate(left.throughput_per_day),
      right ? predRate(right.throughput_per_day) : '—',
      compare && compare.throughput_per_day),
    row('lead time · median', predHours(left.lead_hours.median),
      right ? predHours(right.lead_hours.median) : '—',
      compare && compare.lead_hours, 'arrival → merge'),
  ].join('');
  const bottleneck = `<tr>
    <td class="c-metric">bottleneck</td>
    <td class="c-base">${esc((base.bottleneck && base.bottleneck.person) || '–')}
      ${base.bottleneck ? `<span class="policy-row-note">${predPercent(base.bottleneck.utilisation)} busy</span>` : ''}</td>
    <td class="c-scen">${scenario
      ? `${esc((scenario.bottleneck && scenario.bottleneck.person) || '–')}
        ${scenario.bottleneck ? `<span class="policy-row-note">${predPercent(scenario.bottleneck.utilisation)} busy</span>` : ''}`
      : '—'}</td>
    <td class="c-delta muted">–</td></tr>`;
  const noise = compare && compare.within_noise
    ? `<p class="policy-note">The cycle-time change is <b>smaller than the run-to-run spread</b>
        of the model (${predHours(left.spread && left.spread.median_min)} –
        ${predHours(left.spread && left.spread.median_max)} across
        ${esc(left.spread && left.spread.replications)} replications), so read it as
        “no clear effect”, not as a win.</p>`
    : '';
  return `<table class="table policy-table policy-ab"><thead>${head}</thead>
    <tbody>${rows}${bottleneck}</tbody></table>${noise}`;
}

/** Per-server utilisation: who is busy, and where the queue is. */
export function resourcesMarkup(result) {
  if (!result || !result.resources) return '';
  const rows = result.resources.map((resource) => {
    const state = resource.utilisation >= 0.85 ? 'overloaded'
      : resource.utilisation >= 0.5 ? 'slow' : 'healthy';
    return `<tr>
      <td class="c-who">${esc(resource.person)}</td>
      <td class="c-role">${esc(resource.role)}</td>
      <td><span class="pill ${state}">${predPercent(resource.utilisation)}</span></td>
      <td class="c-dur">${predHours(resource.mean_wait_hours)}</td>
      <td class="c-dur">≈${resource.peak_queue}</td>
      <td class="c-dur">≈${resource.requests}</td>
    </tr>`;
  }).join('');
  const warnings = (result.warnings || []).map((warning) =>
    `<p class="policy-warning">${esc(warning)}</p>`).join('');
  return `<table class="table compact policy-table"><thead><tr>
      <th class="c-who">server</th><th class="c-role">role</th>
      <th>utilisation</th><th class="c-dur">mean wait</th>
      <th class="c-dur">peak queue</th><th class="c-dur">requests</th>
    </tr></thead><tbody>${rows}</tbody></table>${warnings}`;
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
  const facts = [
    ['PRs opened', `${calibration.prs.n} (${calibration.prs.merged} merged)`],
    ['arrival', `${predRate(arrival.per_business_day)} · ≈${arrival.mean_interarrival_hours} h apart`],
    ['review response', `≈${calibration.review_service.pooled.median} h median`],
    ['rework', `${predPercent(rework.probability)} of PRs · ${predPercent(rework.push_class.content)} content`],
    ['sentinel gate', `≈${sentinel.observed.mean_hours} h observed · ${predPercent(sentinel.utilisation)} busy`],
    ['PR size', `≈${size.commits.median} commits median`],
    ['stack depth', stack.available ? `${predPercent(0)} measured` : 'not measurable (no ref digests)'],
    ['window', `${window.business_days} business days · ${window.business_hours} h`],
  ];
  return `<dl class="facts policy-facts">${facts.map(([key, value]) =>
    `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`;
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
      <h2 class="policy-h">Who is busy</h2>
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
      `baseline ${gate.status || 'unknown'} (${signed(gate.percent_off)} vs measured)_`,
    '',
    '| metric | baseline | scenario | Δ |',
    '| --- | --- | --- | --- |',
    `| cycle median | ${predHours(base.cycle_hours.median)} | ${scenario ? predHours(scenario.cycle_hours.median) : '—'} | ${compare ? signed(compare.cycle_median_hours.percent) : '—'} |`,
    `| cycle p90 | ${predHours(base.cycle_hours.p90)} | ${scenario ? predHours(scenario.cycle_hours.p90) : '—'} | ${compare ? signed(compare.cycle_p90_hours.percent) : '—'} |`,
    `| flow efficiency | ${predPercent(base.flow_efficiency.aggregate)} | ${scenario ? predPercent(scenario.flow_efficiency.aggregate) : '—'} | ${compare ? signed(compare.flow_efficiency.percent) : '—'} |`,
    `| throughput | ${predRate(base.throughput_per_day)} | ${scenario ? predRate(scenario.throughput_per_day) : '—'} | ${compare ? signed(compare.throughput_per_day.percent) : '—'} |`,
    `| bottleneck | ${(data.baseline.bottleneck && data.baseline.bottleneck.person) || '–'} | ${scenario && data.scenario.bottleneck ? data.scenario.bottleneck.person : '—'} | |`,
    '',
    `_Every predicted number is an estimate (≈); measured numbers are not. ${compare && compare.within_noise ? 'The delta is within the model\'s run-to-run spread.' : ''}_`,
  ];
  if (data.blocked) lines.splice(2, 0, `> ${data.blocked}`, '');
  return lines.join('\n');
}

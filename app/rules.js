/* Rules — the Policy-lab page (prd/17): "What should we change first?"
 *
 * A ranked, dense list of suggestions, not a card grid. Every row is a rule:
 * severity tint on the left edge + a tinted pill, the rank and the hours at
 * stake first (numbers first), then the plain-language rule, then the evidence
 * as inline mono facts. Sentence case throughout (prd/10 §8).
 */

import { fmtHours, esc } from './format.js';
import { prSizePanel } from './prsize.js';
import { linkifyTickets, markdownLinkTickets, ticketHref } from './tickets.js';

const SEVERITY_LABEL = {
  critical: 'critical',
  warning: 'warning',
  info: 'info',
};

/** Impact in hours, compact and honest: "≈3.3k h", "≈2 d 4 h". */
export function fmtImpact(hours) {
  const value = Number(hours) || 0;
  if (value >= 1000) return `≈${(value / 1000).toFixed(1).replace(/\.0$/, '')}k h`;
  return `≈${fmtHours(value)}`;
}

/** "median_hours" -> "median", "merged_prs" -> "merged PRs". */
export function evidenceLabel(key) {
  if (key === 'diff_size_pending') return 'diff size';
  // prd/46 §5: the worklog card's headline evidence reads as prose.
  const worklog = {
    points_logged_correlation: 'points ↔ logged r',
    points_active_correlation: 'points ↔ active r',
    points_logged_n: 'stories with points',
    lateness: 'lateness',
  }[key];
  if (worklog) return worklog;
  // prd/43 §1: the review-round cap's evidence reads as prose, not snake_case.
  const cap = {
    rounds_cycle_correlation: 'rounds ↔ cycle r',
    rounds_cycle_n: 'merged PRs',
    hours_per_extra_round: 'per extra round',
    // prd/55 §3: the median states its cohort
    median_rounds_merged: 'median rounds (merged PRs)',
    median_rounds_all: 'median rounds (all PRs)',
  }[key];
  if (cap) return cap;
  // prd/55 §5: the sprint-length flag is evidence next to the delivery share.
  const sprint = {
    sprint_length_days: 'sprint length (calendar days)',
    sprint_length_outlier: 'length an outlier',
    sprint_length_note: 'length flag',
  }[key];
  if (sprint) return sprint;
  // prd/22: the stack evidence reads as prose, not as snake_case.
  const stacked = {
    re_review_hours: 're-review',
    depth_median: 'median depth',
    depth_max: 'max depth',
    critical_path_hours: 'critical path',
    stacked_prs: 'stacked PRs',
  }[key];
  if (stacked) return stacked;
  const label = String(key)
    .replace(/_hours$/, '')
    .replace(/_/g, ' ')
    .replace(/\bprs\b/g, 'PRs')
    .replace(/\bp90\b/g, 'p90');
  return label;
}

/** A concrete number from evidence, formatted by its unit. */
export function evidenceValue(key, value) {
  if (value == null || value === '') return '–';
  if (key === 'diff_size_pending') return value ? 'pending' : 'captured';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'number') {
    // A policy target is fixed, not estimated, so it takes no `≈` (B19).
    if (key === 'target_hours') return fmtHours(value);
    if (key.endsWith('_hours') || key === 'hours_per_round' || key === 'hours_per_refinement') return fmtImpact(value);
    // A share (flow efficiency, waiting share) is a fraction of one, not hours.
    if (key.startsWith('share') || key.endsWith('_share') || key.endsWith('_efficiency')) {
      return `${Math.round(value * 100)}%`;
    }
    return Number.isInteger(value) ? String(value) : String(value);
  }
  return String(value);
}

function evidenceItems(evidence) {
  return Object.entries(evidence || {})
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([key, value]) => ({ key, label: evidenceLabel(key), value: evidenceValue(key, value) }));
}

/* ------------------------------------------------------- evidence drill-down
 *
 * prd/26 P5: a rule is "what to change"; its evidence is "which items". Every
 * metric already measures per-item rows (`metrics.*.rows`, or `prs` for flow
 * efficiency), so a suggestion expands in place into that list — dense, one
 * line per item, each item a link to its ticket page (a PR links the story that
 * owns it). The *whole* list is the evidence, not the five examples the rule
 * title quotes; the render cap is stated with its cost when it bites.
 */
export const DRILL_LIMIT = 200;

/** The per-item list a metric exposes, without knowing its shape in advance. */
export function metricRows(metric) {
  if (!metric || typeof metric !== 'object') return [];
  if (Array.isArray(metric.rows)) return metric.rows;
  // flow_efficiency has carried its per-PR list as `prs` since it was built
  if (Array.isArray(metric.prs)) return metric.prs;
  return [];
}

function drillItem(row, suggestion, base = '') {
  const tickets = Array.isArray(row.tickets) && row.tickets.length
    ? row.tickets
    : (row.key ? [row.key] : []);
  const key = tickets.length ? String(tickets[tickets.length - 1]) : '';
  const kind = row.kind || (row.key ? 'story' : 'pr');
  const label = kind === 'story'
    ? String(row.key || row.object_id || '')
    : String(row.id || row.object_id || row.key || '');
  const metric = suggestion.metric;
  const segment = (suggestion.evidence || {}).segment_key;
  let value = '';
  let detail = '';

  if (metric === 'time_to_first_review') {
    value = row.first_review_hours == null ? 'no review yet' : fmtHours(row.first_review_hours);
    detail = row.reviewer ? `review by ${row.reviewer}` : 'no reviewer action';
  } else if (metric === 'cycle_time') {
    value = fmtHours(row.hours);
    detail = row.censored
      ? (row.declined ? 'declined — censored' : 'open — right-censored')
      : 'merged';
  } else if (metric === 'survival') {
    value = fmtHours(row.hours);
    detail = row.event
      ? 'merged'
      : (row.declined ? 'declined — censored' : 'open — censored at the snapshot');
  } else if (metric === 'flow_efficiency') {
    value = row.efficiency == null ? '–' : `${Math.round(row.efficiency * 100)}%`;
    detail = `${fmtHours(row.elapsed_hours)} elapsed · ${fmtHours(row.active_hours)} active`;
  } else if (metric === 'rework_loops') {
    value = `${row.rounds ?? 0} round${row.rounds === 1 ? '' : 's'}`;
    detail = `${row.pushes ?? 0} push${row.pushes === 1 ? '' : 'es'} · ` +
      `${row.unapprovals ?? 0} unapproval${row.unapprovals === 1 ? '' : 's'}`;
  } else if (metric === 'rebase_waste') {
    value = `${row.unapprovals ?? 0} approval${row.unapprovals === 1 ? '' : 's'} lost`;
    detail = Object.entries(row.lost || {})
      .map(([cause, count]) => `${cause} ${count}`).join(' · ');
  } else if (metric === 'comment_tax') {
    value = row.outcome || 'unresolved';
    detail = row.comment_type || 'unclassified';
  } else if (metric === 'conformance') {
    value = `${row.rounds ?? 0} round${row.rounds === 1 ? '' : 's'}`;
    detail = row.merged_without_approval ? 'merged with no approval' : 'over the round cap';
  } else if (metric === 'sentinel_queue') {
    value = fmtHours(row.hours);
    detail = `${row.person || 'sentinel'} · ${row.waits ?? 1} wait${row.waits === 1 ? '' : 's'}`;
  } else if (metric === 'pr_size') {
    value = `${row.commits ?? 0} commits`;
    detail = row.diff_size == null ? 'diff size not captured' : `${row.diff_size} changed lines`;
  } else if (metric === 'vacation_blocking') {
    value = fmtHours(row.hours);
    detail = `${row.person || ''} ${row.delegated ? 'delegated' : 'no delegation'}`.trim();
  } else if (metric === 'stacked_cascade') {
    value = fmtHours(row.re_review_hours);
    detail = `depth ${row.depth ?? 0} · ${row.rebases ?? 0} rebase${row.rebases === 1 ? '' : 's'}`;
  } else if (metric === 'waiting_time_decomposition') {
    const hours = row.largest_hours != null
      ? row.largest_hours
      : (segment ? (row.segments || {})[segment] : null);
    value = hours == null ? 'not measured' : fmtHours(hours);
    detail = `cycle ${fmtHours(row.cycle_hours)}`;
  } else if (metric === 'carry_over') {
    value = (row.sprints || []).length
      ? `sprints ${row.sprints.join(' → ')}` : 'no sprint';
    detail = [
      row.status ? `${row.status}${row.done_at ? ` · done ${row.done_at.slice(0, 10)}` : ''}` : '',
      row.points == null ? '' : `${row.points} points`,
    ].filter(Boolean).join(' · ');
  } else if (metric === 'sprint_delivery') {
    value = row.status === 'late'
      ? fmtHours(row.slip_hours)
      : 'still open';
    detail = `S${row.sprint_id} · ${row.status}${row.points == null ? '' : ` · ${row.points} points`}`;
  } else if (metric === 'sentinel_tail') {
    value = fmtHours(row.hours);
    detail = `only ${row.person || 'sentinel'} · since ${String(row.waiting_since || '').slice(0, 10)}`;
  } else if (metric === 'stale_prs') {
    value = row.age_business_days == null ? fmtHours(row.age_hours) : `${row.age_business_days} business days`;
    detail = (row.reasons || []).join(' · ');
  } else if (metric === 'point_churn') {
    value = `${row.events ?? 0} re-point${row.events === 1 ? '' : 's'}`;
    detail = row.points == null ? '' : `${row.points} points`;
  } else if (metric === 'worklog_cross_check') {
    value = fmtHours(row.logged_hours);
    detail = `${row.worklogs ?? 0} worklog${row.worklogs === 1 ? '' : 's'} · ` +
      `${row.late_worklogs ?? 0} late`;
  } else if (metric === 'jira_conformance') {
    value = (row.violations || []).join(', ');
    detail = [row.started ? `started ${row.started}` : '',
      row.refined ? `refined ${row.refined}` : ''].filter(Boolean).join(' · ');
  } else if (metric === 'reviewer_responsiveness') {
    value = fmtHours(row.hours);
    detail = `${row.person || ''} · ${row.waits ?? 1} wait${row.waits === 1 ? '' : 's'}`.trim();
  } else {
    value = row.hours != null ? fmtHours(row.hours) : '';
    detail = row.state || '';
  }

  return {
    kind,
    key,
    label,
    value,
    detail,
    affected: Boolean(row.affected),
    href: key ? ticketHref(key, base) : '',
    metric,
  };
}

/** Every item behind a suggestion, affected first (the API already ordered). */
export function ruleItems(suggestion, metrics = {}, base = '') {
  if (!suggestion) return [];
  const metric = metrics[suggestion.metric] || {};
  const scope = suggestion.scope || {};
  let rows = metricRows(metric);
  if (scope.kind === 'person' && scope.person) {
    // a person-scoped rule drills into *that person's* items, not the team's
    rows = rows.filter((row) => row.person === scope.person);
  }
  if (scope.kind === 'sprint' && scope.sprint != null) {
    // prd/41 B8: the headline names one sprint, so the drill-down is that
    // sprint's items — not every closed sprint's.
    rows = rows.filter((row) => String(row.sprint_id) === String(scope.sprint));
  }
  // prd/37 §3: "affected first" must be literally true — sort by the wait
  // hours the row carries (the impact), not by key. A metric's own order is
  // the tie-break.
  rows = rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => (
      impactOf(b.row, suggestion.metric) - impactOf(a.row, suggestion.metric)
      || a.index - b.index
    ))
    .map((entry) => entry.row);
  return rows.map((row) => drillItem(row, suggestion, base)).filter((item) => item.label);
}

/**
 * The impact (wait-hours) a drill-down row carries, whatever its metric.
 *
 * prd/37 §3: the drill-down is sorted by this number, so "affected first" is
 * the worst item, not row 15 of 85. A metric that measures a count (rounds,
 * commits, re-points) uses that count; a row with no number sorts last.
 */
export function impactOf(row, metric) {
  if (!row || typeof row !== 'object') return 0;
  const pick = (...keys) => {
    for (const key of keys) {
      const value = Number(row[key]);
      if (Number.isFinite(value)) return value;
    }
    return null;
  };
  const byMetric = {
    time_to_first_review: () => pick('first_review_hours'),
    cycle_time: () => pick('hours'),
    survival: () => pick('hours'),
    flow_efficiency: () => pick('waiting_hours', 'elapsed_hours'),
    reviewer_responsiveness: () => pick('hours'),
    sentinel_queue: () => pick('hours'),
    sentinel_tail: () => pick('hours'),
    vacation_blocking: () => pick('hours'),
    stale_prs: () => pick('hours', 'age_hours'),
    rework_loops: () => pick('rounds'),
    rebase_waste: () => pick('unapprovals'),
    conformance: () => pick('rounds'),
    comment_tax: () => pick('wasted', 'rounds'),
    pr_size: () => pick('commits'),
    stacked_cascade: () => pick('re_review_hours'),
    waiting_time_decomposition: () => pick('largest_hours'),
    carry_over: () => pick('wait_hours', 'slip_hours'),
    sprint_delivery: () => pick('slip_hours'),
    point_churn: () => pick('events'),
    worklog_cross_check: () => pick('logged_hours'),
    jira_conformance: () => pick('violations'),
  };
  const value = (byMetric[metric] || (() => pick(
    'hours', 'wait_hours', 'slip_hours', 'age_hours', 'first_review_hours',
  )))();
  return value == null ? 0 : value;
}

function drillMarkup(suggestion, metrics, base) {
  const items = ruleItems(suggestion, metrics, base);
  if (!items.length) return '';
  const shown = items.slice(0, DRILL_LIMIT);
  const affected = items.filter((item) => item.affected).length;
  const count = `${items.length} item${items.length === 1 ? '' : 's'}`;
  const hint = [
    affected ? `${affected} affected` : '',
    // prd/37 §3: the rows are sorted by the wait they carry, worst first
    'worst wait first',
    'every item, each a link',
  ].filter(Boolean).join(' · ');
  const rows = shown.map((item) => {
    const link = item.href
      ? `<a class="drill-link" href="${esc(item.href)}" data-ticket="${esc(item.key)}"
           title="open ${esc(item.key)}${item.kind === 'pr' ? ' — the ticket that owns this PR' : ''}">${esc(item.label)}</a>`
      : `<span class="mono">${esc(item.label)}</span>`;
    const keyTag = item.key && item.kind !== 'story'
      ? `<span class="drill-key mono muted">${esc(item.key)}</span>` : '';
    return `<tr class="${item.affected ? 'affected' : ''}" data-drill-item="${esc(item.label)}">
      <td class="drill-item" data-label="">${link}${keyTag}</td>
      <td class="drill-detail" data-label="detail">${esc(item.detail)}</td>
      <td class="drill-value mono" data-label="measured">${esc(item.value)}</td>
    </tr>`;
  }).join('');
  const cap = items.length > shown.length
    ? `<p class="drill-more">showing the first ${shown.length} of ${items.length} — the
        full list is in <span class="mono">metrics.${esc(suggestion.metric)}.rows</span></p>`
    : '';
  return `<details class="rule-drill" data-rule-drill="${esc(suggestion.id)}">
    <summary><span>${count}</span>
      <span class="drill-hint">${hint}</span></summary>
    <div class="drill-wrap">
      <table class="drill">
        <thead><tr><th>item</th><th>detail</th><th class="num">measured</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    ${cap}
  </details>`;
}

/**
 * prd/37 §2: rules that price the same hours share a cost pool. The headline
 * counts a pool once; a rule that is fully absorbed by a more specific one
 * says where its hours were counted, so a reader cannot add the list up twice.
 */
export function stakeNote(suggestion) {
  const stake = suggestion && suggestion.at_stake;
  if (!stake || stake.pool_rules < 2) return '';
  // the rule whose estimate *is* the pool ceiling is the broadest lens: it says
  // the pool is counted once, rather than "counted in" a narrower rule.
  if (stake.hours >= stake.pool_hours) {
    return `<span class="rule-stake-note" title="the headline counts this pool once, at ${esc(fmtImpact(stake.pool_hours))}">pool ${esc(fmtImpact(stake.pool_hours))}</span>`;
  }
  if (stake.attributed_to) {
    return `<span class="rule-stake-note" title="this rule prices part of the ${esc(stake.pool)} pool; the headline counts that pool once">counted in #${esc(stake.attributed_to_rank ?? '–')}</span>`;
  }
  return '';
}

/* prd/43 §1: the Rules ↔ Policy-lab cross-check.
 *
 * A rule and the calibrated model must not disagree silently. When the lab's
 * preset scenario bears on a rule's advice, the rule carries the lab's own
 * verdict: a contradicted policy says "not a measured win", a no-clear-effect
 * policy says so, and a supported one is the lever the page ranks on. The line
 * is plain text from the API (`policy_lab.note`), so the two surfaces cannot
 * drift into two phrasings. */
export function labNote(suggestion) {
  const lab = suggestion && suggestion.policy_lab;
  if (!lab || !lab.note) return '';
  const tone = lab.contradiction ? 'contradiction' : lab.supports ? 'supports' : 'neutral';
  return `<p class="rule-lab ${tone}" data-lab-policy="${esc(lab.policy || '')}"${
    lab.best ? ' data-lab-best-rule="true"' : ''}
    title="the policy lab's calibrated model on ${esc(lab.label || 'this policy')}">
    <span class="lab-tag mono">policy lab${lab.best ? ' · best scenario' : ''}</span> ${esc(lab.note)}</p>`;
}

/** prd/43 §1: "the lab's best scenario is X" — the one cross-reference line. */
export function labBestLine(data) {
  const lab = data && data.policy_lab;
  if (!lab || !lab.available || !lab.best) return '';
  return lab.note || '';
}

export function ruleRow(suggestion, { base = '', metrics = {} } = {}) {
  const severity = SEVERITY_LABEL[suggestion.severity] || 'info';
  const facts = evidenceItems(suggestion.evidence);
  const person = suggestion.scope && suggestion.scope.kind === 'person'
    ? suggestion.scope.person : null;
  const note = stakeNote(suggestion);
  // prd/41 B4(b): an at-stake figure derived from an assumption says so; it is
  // not a measured saving.
  const assumed = Boolean((suggestion.evidence || {}).impact_assumed);
  const stakeTitle = assumed
    ? `assumed hours removed by this rule (${esc((suggestion.evidence || {}).impact_basis || 'an assumption')}); not a measured saving`
    : `estimated hours removed by this rule (impact × confidence ${esc(suggestion.confidence)}); the headline counts each cost pool once`;
  return `
    <article class="rule sev-${esc(severity)}" data-rule="${esc(suggestion.id)}">
      <div class="rule-rank mono" aria-hidden="true">${String(suggestion.rank).padStart(2, '0')}</div>
      <div class="rule-main">
        <div class="rule-top">
          <span class="pill ${esc(severity)}">${esc(severity)}</span>
          <span class="rule-metric mono">${esc(suggestion.metric)}</span>
          ${person ? `<span class="rule-scope mono" title="pseudonym — the store holds no raw names">${esc(person)}</span>` : ''}
          <span class="grow"></span>
          <span class="rule-impact num" title="${stakeTitle}">${fmtImpact(suggestion.impact_hours)} at stake${assumed ? ' <span class="muted">(assumed)</span>' : ''}</span>
          ${note}
        </div>
        <h2 class="rule-title">${esc(suggestion.title)}</h2>
        <p class="rule-action">${esc(suggestion.suggestion)}</p>
        ${assumed ? `<p class="rule-assumed">at stake is an assumption (${esc(
          (suggestion.evidence || {}).impact_basis || 'an assumption')}) — not a measured saving.</p>` : ''}
        ${labNote(suggestion)}
        <dl class="rule-evidence">
          ${facts.map((fact) => `<div class="fact"><dt>${esc(fact.label)}</dt>
            <dd class="mono">${linkifyTickets(fact.value, base)}</dd></div>`).join('')}
        </dl>
        ${drillMarkup(suggestion, metrics, base)}
      </div>
    </article>`;
}

export function rulesStats(data) {
  const suggestions = (data && data.suggestions) || [];
  const critical = suggestions.filter((s) => s.severity === 'critical').length;
  const stake = (data && data.at_stake) || null;
  // prd/37 §2: the additive headline — never the sum of the per-rule estimates
  const impact = stake && stake.hours != null
    ? Number(stake.hours) || 0
    : suggestions.reduce((sum, s) => sum + (Number(s.impact_hours) || 0), 0);
  const overlap = stake && stake.overlap_hours ? Number(stake.overlap_hours) : 0;
  const sample = (data && data.sample) || {};
  const dataset = (data && data.dataset) || {};
  const rules = suggestions.length;
  const prs = sample.prs ?? 0;
  const datasetPrs = dataset.prs ?? null;
  const outside = dataset.prs_outside_window ?? 0;
  // prd/55 §6: "124 of 131 PRs" — the window is not the dataset, and the header
  // must not let the dataset census stand for the cohort it dropped.
  const prsLabel = datasetPrs != null && outside
    ? `<b>${prs}</b> of ${datasetPrs} PRs (${outside} opened before the window)`
    : `<b>${prs}</b> PR${prs === 1 ? '' : 's'}`;
  // prd/21: the ticket cohort travels next to the PR cohort; a store without the
  // Jira ingest shows no story count rather than a misleading zero.
  const stories = (data && data.jira && data.jira.available) ? (sample.stories ?? 0) : null;
  return `<span class="stat-inline"><b>${rules}</b> rule${rules === 1 ? '' : 's'}</span>
    <span class="stat-inline"><b class="${critical ? 'attention' : ''}">${critical}</b> critical</span>
    <span class="stat-inline" title="${esc(overlap
      ? `counted once: ${fmtImpact(overlap)} of overlapping rule estimates is not added again`
      : 'each at-stake hour is attributed to one rule')}"><b>${fmtImpact(impact)}</b> at stake${
      overlap ? ` <span class="muted">(counted once)</span>` : ''}</span>
    <span class="stat-inline">${prsLabel}${
      stories === null ? '' : ` · <b>${stories}</b> stor${stories === 1 ? 'y' : 'ies'}`} ·
      <b>${sample.days ?? 0}</b> of ${dataset.days ?? '–'} calendar days${
      outside ? ` <span class="muted">(window)</span>` : ''}</span>`;
}

/* B16: the `people` level is not a no-op toggle. When the engine has no
 * person-scoped signal, say so instead of silently listing team rules under a
 * person heading. */
export function personSignalNote(data, { below = true } = {}) {
  if (!data || data.level !== 'person') return '';
  const personRules = (data.suggestions || [])
    .filter((suggestion) => suggestion.scope && suggestion.scope.kind === 'person');
  if (personRules.length) return '';
  return 'No person-level rule crosses its threshold in this window' +
    (below ? '; the team rules below still apply.' : '.');
}

export function rulesMarkup(data, { base = '', prSize = null, caps = null } = {}) {
  const suggestions = (data && data.suggestions) || [];
  const note = personSignalNote(data, { below: suggestions.length > 0 });
  const lab = labBestLine(data);
  if (!suggestions.length) {
    return `<div class="rules-empty">
      ${note ? `<p>${esc(note)}</p>` : ''}
      ${lab ? `<p class="rules-lab" data-lab-best>${esc(lab)}</p>` : ''}
      <p>No rule crosses its threshold in this window.</p>
      <p class="muted">That is a result, not a missing screen: first review under 24 h, cycle
        time under 9 d (72 business hours), two review rounds or fewer, no lost approvals.
        Widen the timeframe or switch to people to check the per-person rules.</p>
    </div>
    ${prSizePanel(prSize, { caps })}`;
  }
  // prd/39: the calibration panel sits next to the size rule (or after the list
  // when the rule does not fire), so the threshold the rule quotes is adjustable
  // exactly where it is read.
  const rows = suggestions.map((suggestion) =>
    ruleRow(suggestion, { base, metrics: (data && data.metrics) || {} }));
  const panel = prSizePanel(prSize, { caps });
  if (panel) {
    const index = suggestions.findIndex((suggestion) => suggestion.metric === 'pr_size');
    if (index >= 0) rows.splice(index + 1, 0, panel);
    else rows.push(panel);
  }
  return `${note ? `<p class="rules-note">${esc(note)}</p>` : ''}
    ${lab ? `<p class="rules-lab" data-lab-best>${esc(lab)}</p>` : ''}
    <div class="rules-list">${rows.join('')}</div>`;
}

export function rulesMarkdown(data, base = '') {
  if (!data) return '';
  const note = personSignalNote(data, { below: (data.suggestions || []).length > 0 });
  const personRules = (data.suggestions || [])
    .some((suggestion) => suggestion.scope && suggestion.scope.kind === 'person');
  // B16: the heading must not claim "(person)" over team rules.
  const level = data.level === 'person'
    ? (personRules ? 'person' : 'team — no person-level signal in this window')
    : (data.level || 'team');
  const dataset = data.dataset || {};
  const outside = dataset.prs_outside_window || 0;
  const lines = [
    `# Rules — what should we change first? (${level})`,
    '',
    `_${data.sample.prs}${outside ? ` of ${dataset.prs}` : ''} PR` +
      `${data.sample.prs === 1 && !outside ? '' : 's'} opened over ` +
      `${data.sample.days} calendar day${data.sample.days === 1 ? '' : 's'}` +
      (outside ? ` of a ${dataset.days}-day dataset — ${outside} PR${outside === 1 ? '' : 's'} ` +
        'opened before the window and is not in the cohort' : '') + ` · ` +
      `${data.hours_mode === 'business' ? 'business hours (1 d = 8 h)' : 'wall-clock hours'} · ` +
      `snapshot ${data.as_of}_`,
    '',
  ];
  if (note) lines.push(`_${note}_`, '');
  const stake = data.at_stake || null;
  if (stake) {
    lines.push(`_${fmtImpact(stake.hours)} at stake — counted once across ` +
      `${(stake.pools || []).length} cost pool${(stake.pools || []).length === 1 ? '' : 's'}; ` +
      `${fmtImpact(stake.overlap_hours)} of overlapping estimates is not added._`, '');
  }
  const lab = labBestLine(data);
  if (lab) lines.push(`_${lab}._`, '');
  for (const suggestion of data.suggestions) {
    const facts = evidenceItems(suggestion.evidence)
      .map((fact) => `${fact.label} ${markdownLinkTickets(fact.value, base)}`).join(' · ');
    const shared = suggestion.at_stake && suggestion.at_stake.attributed_to
      ? ` — counted in #${suggestion.at_stake.attributed_to_rank}` : '';
    lines.push(
      `## ${suggestion.rank}. ${suggestion.title}`,
      '',
      `- severity: ${suggestion.severity}`,
      `- metric: ${suggestion.metric}`,
      `- impact: ${fmtImpact(suggestion.impact_hours)} (confidence ${suggestion.confidence})${shared}${
        (suggestion.evidence || {}).impact_assumed ? ' — assumed, not measured' : ''}`,
      `- rule: ${suggestion.suggestion}`,
      ...(suggestion.policy_lab && suggestion.policy_lab.note
        ? [`- policy lab${suggestion.policy_lab.best ? ' (best scenario)' : ''}: ${suggestion.policy_lab.note}`] : []),
      `- evidence: ${facts}`,
    );
    // prd/26 P5: the exported rule carries its items too, not only the count.
    const items = ruleItems(suggestion, data.metrics || {});
    if (items.length) {
      const shown = items.slice(0, 25).map((item) => (item.key
        ? `[${item.label}](${ticketHref(item.key, base)})` : item.label));
      lines.push(
        `- items (${items.length}${items.length > 25 ? `, first 25` : ''}): ${shown.join(', ')}`,
      );
    }
    lines.push('');
  }
  lines.push(`_${data.note || ''}_`);
  return lines.join('\n');
}

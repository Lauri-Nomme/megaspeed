/* Rules — the Policy-lab page (prd/17): "What should we change first?"
 *
 * A ranked, dense list of suggestions, not a card grid. Every row is a rule:
 * severity tint on the left edge + a tinted pill, the rank and the hours at
 * stake first (numbers first), then the plain-language rule, then the evidence
 * as inline mono facts. Sentence case throughout (prd/10 §8).
 */

import { fmtHours, esc } from './format.js';
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
    detail = row.points == null ? '' : `${row.points} points`;
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
  // affected first, keeping each metric's own order inside the two groups
  rows = rows.slice().sort((a, b) => Number(Boolean(b.affected)) - Number(Boolean(a.affected)));
  return rows.map((row) => drillItem(row, suggestion, base)).filter((item) => item.label);
}

function drillMarkup(suggestion, metrics, base) {
  const items = ruleItems(suggestion, metrics, base);
  if (!items.length) return '';
  const shown = items.slice(0, DRILL_LIMIT);
  const affected = items.filter((item) => item.affected).length;
  const count = `${items.length} item${items.length === 1 ? '' : 's'}`;
  const hint = [
    affected ? `${affected} affected first` : '',
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

export function ruleRow(suggestion, { base = '', metrics = {} } = {}) {
  const severity = SEVERITY_LABEL[suggestion.severity] || 'info';
  const facts = evidenceItems(suggestion.evidence);
  const person = suggestion.scope && suggestion.scope.kind === 'person'
    ? suggestion.scope.person : null;
  return `
    <article class="rule sev-${esc(severity)}" data-rule="${esc(suggestion.id)}">
      <div class="rule-rank mono" aria-hidden="true">${String(suggestion.rank).padStart(2, '0')}</div>
      <div class="rule-main">
        <div class="rule-top">
          <span class="pill ${esc(severity)}">${esc(severity)}</span>
          <span class="rule-metric mono">${esc(suggestion.metric)}</span>
          ${person ? `<span class="rule-scope mono" title="pseudonym — the store holds no raw names">${esc(person)}</span>` : ''}
          <span class="grow"></span>
          <span class="rule-impact num" title="estimated hours removed by this rule (impact × confidence ${esc(suggestion.confidence)})">${fmtImpact(suggestion.impact_hours)} at stake</span>
        </div>
        <h2 class="rule-title">${esc(suggestion.title)}</h2>
        <p class="rule-action">${esc(suggestion.suggestion)}</p>
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
  const impact = suggestions.reduce((sum, s) => sum + (Number(s.impact_hours) || 0), 0);
  const sample = (data && data.sample) || {};
  const rules = suggestions.length;
  const prs = sample.prs ?? 0;
  // prd/21: the ticket cohort travels next to the PR cohort; a store without the
  // Jira ingest shows no story count rather than a misleading zero.
  const stories = (data && data.jira && data.jira.available) ? (sample.stories ?? 0) : null;
  return `<span class="stat-inline"><b>${rules}</b> rule${rules === 1 ? '' : 's'}</span>
    <span class="stat-inline"><b class="${critical ? 'attention' : ''}">${critical}</b> critical</span>
    <span class="stat-inline"><b>${fmtImpact(impact)}</b> at stake</span>
    <span class="stat-inline"><b>${prs}</b> PR${prs === 1 ? '' : 's'}${
      stories === null ? '' : ` · <b>${stories}</b> stor${stories === 1 ? 'y' : 'ies'}`} ·
      <b>${sample.days ?? 0}</b> d</span>`;
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

export function rulesMarkup(data, { base = '' } = {}) {
  const suggestions = (data && data.suggestions) || [];
  const note = personSignalNote(data, { below: suggestions.length > 0 });
  if (!suggestions.length) {
    return `<div class="rules-empty">
      ${note ? `<p>${esc(note)}</p>` : ''}
      <p>No rule crosses its threshold in this window.</p>
      <p class="muted">That is a result, not a missing screen: first review under 24 h, cycle
        time under 3 d, two review rounds or fewer, no lost approvals. Widen the timeframe or
        switch to people to check the per-person rules.</p>
    </div>`;
  }
  return `${note ? `<p class="rules-note">${esc(note)}</p>` : ''}
    <div class="rules-list">${suggestions.map((suggestion) =>
      ruleRow(suggestion, { base, metrics: (data && data.metrics) || {} })).join('')}</div>`;
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
  const lines = [
    `# Rules — what should we change first? (${level})`,
    '',
    `_${data.sample.prs} PR${data.sample.prs === 1 ? '' : 's'} opened over ` +
      `${data.sample.days} day${data.sample.days === 1 ? '' : 's'} · ` +
      `${data.hours_mode === 'business' ? 'business hours' : 'wall-clock hours'} · ` +
      `snapshot ${data.as_of}_`,
    '',
  ];
  if (note) lines.push(`_${note}_`, '');
  for (const suggestion of data.suggestions) {
    const facts = evidenceItems(suggestion.evidence)
      .map((fact) => `${fact.label} ${markdownLinkTickets(fact.value, base)}`).join(' · ');
    lines.push(
      `## ${suggestion.rank}. ${suggestion.title}`,
      '',
      `- severity: ${suggestion.severity}`,
      `- metric: ${suggestion.metric}`,
      `- impact: ${fmtImpact(suggestion.impact_hours)} (confidence ${suggestion.confidence})`,
      `- rule: ${suggestion.suggestion}`,
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

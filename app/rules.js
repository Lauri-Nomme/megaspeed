/* Rules — the Policy-lab page (prd/17): "What should we change first?"
 *
 * A ranked, dense list of suggestions, not a card grid. Every row is a rule:
 * severity tint on the left edge + a tinted pill, the rank and the hours at
 * stake first (numbers first), then the plain-language rule, then the evidence
 * as inline mono facts. Sentence case throughout (prd/10 §8).
 */

import { fmtHours, esc } from './format.js';
import { linkifyTickets, markdownLinkTickets } from './tickets.js';

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

export function ruleRow(suggestion, { base = '' } = {}) {
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
    <div class="rules-list">${suggestions.map((suggestion) => ruleRow(suggestion, { base })).join('')}</div>`;
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
      '',
    );
  }
  lines.push(`_${data.note || ''}_`);
  return lines.join('\n');
}

/* Rules — the Policy-lab page (prd/17): "What should we change first?"
 *
 * A ranked, dense list of suggestions, not a card grid. Every row is a rule:
 * severity tint on the left edge + a tinted pill, the rank and the hours at
 * stake first (numbers first), then the plain-language rule, then the evidence
 * as inline mono facts. Sentence case throughout (prd/10 §8).
 */

import { fmtHours, esc } from './format.js';

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
    if (key.endsWith('_hours') || key === 'hours_per_round') return fmtImpact(value);
    if (key.startsWith('share')) return `${Math.round(value * 100)}%`;
    return Number.isInteger(value) ? String(value) : String(value);
  }
  return String(value);
}

function evidenceItems(evidence) {
  return Object.entries(evidence || {})
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => ({ key, label: evidenceLabel(key), value: evidenceValue(key, value) }));
}

export function ruleRow(suggestion) {
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
            <dd class="mono">${esc(fact.value)}</dd></div>`).join('')}
        </dl>
      </div>
    </article>`;
}

export function rulesStats(data) {
  const suggestions = (data && data.suggestions) || [];
  const critical = suggestions.filter((s) => s.severity === 'critical').length;
  const impact = suggestions.reduce((sum, s) => sum + (Number(s.impact_hours) || 0), 0);
  const sample = (data && data.sample) || {};
  return `<span class="stat-inline"><b>${suggestions.length}</b> rules</span>
    <span class="stat-inline"><b class="${critical ? 'attention' : ''}">${critical}</b> critical</span>
    <span class="stat-inline"><b>${fmtImpact(impact)}</b> at stake</span>
    <span class="stat-inline"><b>${sample.prs ?? 0}</b> PRs · <b>${sample.days ?? 0}</b> d</span>`;
}

export function rulesMarkup(data) {
  const suggestions = (data && data.suggestions) || [];
  if (!suggestions.length) {
    return `<div class="rules-empty">
      <p>No rule crosses its threshold in this window.</p>
      <p class="muted">That is a result, not a missing screen: first review under 24 h, cycle
        time under 3 d, two review rounds or fewer, no lost approvals. Widen the timeframe or
        switch to people to check the per-person rules.</p>
    </div>`;
  }
  return `<div class="rules-list">${suggestions.map(ruleRow).join('')}</div>`;
}

export function rulesMarkdown(data) {
  if (!data) return '';
  const lines = [
    `# Rules — what should we change first? (${data.level || 'team'})`,
    '',
    `_${data.sample.prs} PRs opened over ${data.sample.days} days · ` +
      `${data.hours_mode === 'business' ? 'business hours' : 'wall-clock hours'} · ` +
      `snapshot ${data.as_of}_`,
    '',
  ];
  for (const suggestion of data.suggestions) {
    const facts = evidenceItems(suggestion.evidence)
      .map((fact) => `${fact.label} ${fact.value}`).join(' · ');
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

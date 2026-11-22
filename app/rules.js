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
import { reworkMarkdown, reworkPanel } from './rework.js';
import { worklogMarkdown, worklogPanel, fmtRate } from './worklog.js';
import { firstPassPanels, yieldMarkdown } from './yield.js';
import {
  verdictBanner, isDisclosedByDefault, currentRung, ruleRung,
} from './verdict.js';

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

/** Evidence labels a reader can use: no snake_case, no internal doc nouns. */

//: prd/63 V-B: the closed segment vocabulary, in the words the ticket page uses.
const SEGMENT_LABELS = {
  created_ready: 'created → ready',
  ready_sprint: 'ready → sprint',
  sprint_in_progress: 'sprint → in progress',
  in_progress_pr_open: 'in progress → PR open',
  pr_open_reviewer_added: 'PR open → reviewer added',
  pr_open_first_review: 'waiting for a first review',
  review_rounds: 'review rounds',
  final_approval_merge: 'final approval → merge',
  merge_done: 'merge → done',
  merge_verification: 'merge → verification',
  verification_done: 'verification → done',
};

export function segmentLabel(key) {
  return SEGMENT_LABELS[String(key)] || String(key || '').replace(/_/g, ' ');
}

//: prd/63 V-B: a metric id is an identifier; the rule card shows the question.
const METRIC_LABELS = {
  sprint_delivery: 'sprint delivery',
  carry_over: 'carry-over',
  comment_tax: 'comment tax',
  rework_loops: 'review rounds',
  survival: 'time to merge',
  stale_prs: 'stale PRs',
  worklog_cross_check: 'worklog',
  waiting_time_decomposition: 'waiting time',
  flow_efficiency: 'flow efficiency',
  sentinel_tail: 'sentinel tail',
  pr_size: 'PR size',
  time_to_first_review: 'first review',
  cycle_time: 'cycle time',
  reviewer_responsiveness: 'reviewer response',
  rebase_waste: 'rebase waste',
  conformance: 'conformance',
  sentinel_queue: 'sentinel queue',
  vacation_blocking: 'vacation',
  stacked_cascade: 'stacked PRs',
  point_churn: 'point churn',
  jira_conformance: 'Jira conformance',
};

export function metricLabel(id) {
  return METRIC_LABELS[String(id)] || String(id || '').replace(/_/g, ' ');
}

export function evidenceLabel(key) {
  if (key === 'diff_size_pending') return 'diff size';
  // prd/63 F28/V-B: the decomposition's segment key is a fact, not an
  // identifier: "created_ready" reads as "biggest segment".
  if (key === 'segment_key') return 'biggest segment';
  // prd/63 V-A10: the relationship itself is a labelled fact.
  if (key === 'relationship') return 'relationship';
  // prd/46 §5: the worklog card's headline evidence reads as prose.
  const worklog = {
    points_logged_correlation: 'points ↔ logged r',
    points_active_correlation: 'points ↔ active r',
    points_logged_n: 'stories with points',
    median_logged_hours: 'median logged',
    median_hours_per_point: 'median per point',
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
    // prd/67 W-05: the window's own sprint count, beside the store's scope.
    window_sprints: 'sprints closed in this window',
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

/** A key that names a duration in hours, whatever its shape.
 *
 * prd/64 F-18: `hours_per_round`, `hours_per_refinement`, `hours_per_extra_round`
 * and a bare `hours` are one unit — the single duration language (`≈6 h 1 min`).
 * Matching on the *word* rather than a hand-listed set means a new evidence key
 * cannot smuggle a raw float back onto a card.
 */
export function isDurationKey(key) {
  return /(^|_)hours?($|_)/.test(String(key || ''));
}

/** A key that names a count of days, not a pool of hours.
 *
 * prd/68 F-05: `window_days: 1880.4`, `limit_days: 2.93` and
 * `sprint_length_days` are day counts. Passing one to `fmtImpact` printed
 * `≈1.9k h` — a day count wearing the hour unit.
 */
export function isDaysKey(key) {
  return /(^|_)days?($|_)/.test(String(key || ''));
}

/** A day count with its unit: `1880.4 calendar days`.
 *
 * `limit_days` is derived from the stale-PR threshold in business hours
 * (`analysis/rules.py`: `limit_hours / BUSINESS_HOURS_PER_DAY`), so it keeps
 * the business-day unit; window and sprint lengths are calendar days.
 */
export function fmtDayCount(key, value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const shown = Number(number.toFixed(1));
  const unit = key === 'limit_days' ? 'business days' : 'calendar days';
  return `${shown} ${shown === 1 ? unit.replace(/s$/, '') : unit}`;
}

/** A concrete number from evidence, formatted by its unit. */
export function evidenceValue(key, value) {
  if (value == null || value === '') return '–';
  if (key === 'diff_size_pending') return value ? 'pending' : 'captured';
  // prd/63 V-B: closed-vocabulary keys are rendered as their human segment
  // names, not as `created_ready`.
  if (key === 'segment_key') return segmentLabel(value);
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'number') {
    // A policy target is fixed, not estimated, so it takes no `≈` (B19).
    if (key === 'target_hours') return fmtHours(value);
    // prd/53 §1: a measured median is not an estimate, and hours per point is a
    // rate rather than a duration.
    if (key === 'median_logged_hours') return fmtHours(value);
    if (key === 'median_hours_per_point') return fmtRate(value);
    // prd/64 F-18: every hours-shaped key is a duration, including the bare
    // `hours` and `hours_per_extra_round` the cards used to print as `283.6`.
    if (isDurationKey(key)) return fmtImpact(value);
    // A share (flow efficiency, waiting share) is a fraction of one, not hours.
    if (key.startsWith('share') || key.endsWith('_share') || key.endsWith('_efficiency')) {
      return `${Math.round(value * 100)}%`;
    }
    if (Number.isInteger(value)) return String(value);
    // prd/68 F-05: a day count is a day count — `window_days: 1880.4` must
    // read "1880.4 calendar days", never `≈1.9k h`.
    if (isDaysKey(key)) return fmtDayCount(key, value);
    // prd/64 F-18 guard: a key that *looks* like a duration but slipped past
    // `isDurationKey` (e.g. `hours_to_merge`) still must not print `6.01`.
    if (/(^|_)(hour|minute|duration)/.test(String(key))) return fmtImpact(value);
    return String(value);
  }
  return String(value);
}

function evidenceItems(evidence) {
  const store = {};
  const items = [];
  for (const [key, value] of Object.entries(evidence || {})) {
    if (value === null || value === undefined || value === '') continue;
    // prd/67 W-05: the two store-wide carry-over numbers are one fact, placed
    // next to the window count — not two snake_case chips a reader must add up.
    if (key === 'store_carried_over' || key === 'store_sprints') {
      store[key] = value;
      continue;
    }
    items.push({ key, label: evidenceLabel(key), value: evidenceValue(key, value) });
  }
  const scoped = storeScopeItem(store.store_carried_over, store.store_sprints);
  if (scoped) {
    const at = items.findIndex((item) => item.key === 'carried_over');
    items.splice(at >= 0 ? at + 1 : items.length, 0, scoped);
  }
  return items;
}

/** prd/67 W-05: "the whole store: 412 carried over across 106 sprints".
 *
 * The carry-over rule counts the stories created in the chosen window; the
 * sprint lens counts every story in the store. The two scopes sit side by side
 * so the window count cannot be read as the store total. `null` (the store has
 * no closed sprint) renders nothing rather than a zero.
 */
export function storeScopeItem(carried, sprints) {
  if (carried == null || sprints == null) return null;
  return {
    key: 'store_carried_over',
    label: 'the whole store',
    value: `${carried} carried over across ${sprints} ` +
      `sprint${sprints === 1 ? '' : 's'}`,
  };
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
    // prd/63 V-B: one duration language. "2.41 business days" was a second
    // unit beside "2 d 1 h"; the age is business hours and fmtHours spells it.
    value = fmtHours(row.age_hours);
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
  // prd/37 §3: "affected first" must be literally true — the rows the rule is
  // about lead, then the wait hours the row carries (the impact), not its key.
  // prd/64 F-06a: the impact sort alone buried the affected rows whenever the
  // rule's "affected" test is not the sort's number (censoring, worklog,
  // flow efficiency), so affected is the first key and impact the tie-break.
  // A metric's own order is the last tie-break.
  rows = rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => (
      Number(Boolean(b.row.affected)) - Number(Boolean(a.row.affected))
      || impactOf(b.row, suggestion.metric) - impactOf(a.row, suggestion.metric)
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
    ? `<p class="drill-more">showing the first ${shown.length} of ${items.length} items
        (each a link; the export carries the first 25)</p>`
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

/* prd/62: the guardrail beside a lever's speed result.
 *
 * The sentence comes from the API's `policy_lab.guardrail` — built from the
 * *same run* the Policy page shows — so the rule and the lab cannot drift. When
 * the model cannot represent a guardrail the sentence starts with "held
 * constant:" and prints the measured baseline; no lever is left silent. */
export function guardrailLine(suggestion) {
  const guard = suggestion && suggestion.policy_lab
    && suggestion.policy_lab.guardrail;
  if (!guard || !guard.text) return '';
  const parts = (guard.parts && guard.parts.length)
    ? guard.parts
    : [{ text: guard.text, tone: 'neutral' }];
  const body = parts.map((part) => {
    const text = esc(part.text);
    if (part.tone === 'warning') {
      // only the number is tinted, never the whole sentence (prd/62)
      const number = text.replace(
        /(\d[\d.,]*\s*→\s*\d[\d.,]*)/,
        '<b class="guardrail-warn">$1</b>',
      );
      return `<span class="guardrail-fact">${number}</span>`;
    }
    return `<span class="guardrail-fact">${text}</span>`;
  }).join('<span class="guardrail-sep" aria-hidden="true"> · </span>');
  const tail = guard.modelled
    ? ' — the model does not rework the code, only the review loop'
    : ' — the model does not rework the code';
  return `<div class="rule-guardrail" data-guardrail="${
    guard.modelled ? 'modelled' : 'held_constant'}">
    <span class="guardrail-overline">GUARDRAIL</span>
    <p>${body}<span class="guardrail-tail">${esc(tail)}</span></p>
  </div>`;
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
    <article class="rule sev-${esc(severity)}" id="rule-${esc(suggestion.id)}" data-rule="${esc(suggestion.id)}">
      <div class="rule-rank mono" aria-hidden="true">${String(suggestion.rank).padStart(2, '0')}</div>
      <div class="rule-main">
        <div class="rule-top">
          <span class="pill ${esc(severity)}">${esc(severity)}</span>
          <span class="rule-metric mono">${esc(metricLabel(suggestion.metric))}</span>
          ${(suggestion.evidence || {}).relationship === 'correlation'
            ? '<span class="pill neutral" data-relationship="correlation" title="the two numbers move together; that is not proof that one causes the other">correlation</span>'
            : ''}
          ${(suggestion.evidence || {}).live_state
            ? '<span class="pill neutral" data-live-state="true" title="describes the live state at the snapshot, not only the chosen timeframe">live</span>'
            : ''}
          ${person ? `<span class="rule-scope mono" title="pseudonym — the store holds no raw names">${esc(person)}</span>` : ''}
          <span class="grow"></span>
          <span class="rule-impact num" title="${stakeTitle}">${fmtImpact(suggestion.impact_hours)} at stake${assumed ? ' <span class="muted">(assumed)</span>' : ''}</span>
          ${note}
        </div>
        <h2 class="rule-title">${esc(suggestion.title)}</h2>
        <p class="rule-action">${esc(suggestion.suggestion)}</p>
        ${suggestion.policy_lab && suggestion.policy_lab.contradiction
          ? `<p class="rule-caveat" data-lab-contradiction="true">The lab does not support this as a lever (${
            esc(suggestion.policy_lab.note || 'no clear effect')}); treat it as a hypothesis to test, not a proven fix.</p>`
          : ''}
        ${assumed ? `<p class="rule-assumed">at stake is an assumption (${esc(
          (suggestion.evidence || {}).impact_basis || 'an assumption')}) — not a measured saving.</p>` : ''}
        ${labNote(suggestion)}
        ${guardrailLine(suggestion)}
        <dl class="rule-evidence">
          ${facts.map((fact) => `<div class="fact"><dt>${esc(fact.label)}</dt>
            <dd class="mono">${linkifyTickets(fact.value, base)}</dd></div>`).join('')}
        </dl>
        ${drillMarkup(suggestion, metrics, base)}
      </div>
    </article>`;
}

/* prd/63 F28: a person-level list is a filter over the same payload, so every
 * reader of `suggestions` (the list, the stats strip) must filter the same way.
 */
function isPersonRule(suggestion) {
  return Boolean(suggestion && suggestion.scope && suggestion.scope.kind === 'person');
}

export function rulesStats(data) {
  const all = (data && data.suggestions) || [];
  // prd/64 F-05: on the `people (pseudonyms)` tab the strip counted the team
  // rules behind an empty person list ("10 rules · 4 critical · ≈2.6k h" over
  // "no person-level rule crosses its threshold"). The strip follows the tab.
  const personLevel = Boolean(data && data.level === 'person');
  const personRules = all.filter(isPersonRule);
  const suggestions = personLevel ? personRules : all;
  const critical = suggestions.filter((s) => s.severity === 'critical').length;
  const stake = (data && data.at_stake) || null;
  // prd/37 §2: the additive headline — never the sum of the per-rule estimates.
  // A person tab cannot quote the team pool's ceiling, so it adds the person
  // rules' own attributed share (each hour is still counted once).
  const impact = personLevel
    ? suggestions.reduce((sum, s) => {
      const own = s.at_stake && s.at_stake.attributed_hours != null
        ? Number(s.at_stake.attributed_hours) : Number(s.impact_hours);
      return sum + (Number.isFinite(own) ? own : 0);
    }, 0)
    : (stake && stake.hours != null
      ? Number(stake.hours) || 0
      : suggestions.reduce((sum, s) => sum + (Number(s.impact_hours) || 0), 0));
  const overlap = !personLevel && stake && stake.overlap_hours
    ? Number(stake.overlap_hours) : 0;
  const sample = (data && data.sample) || {};
  const dataset = (data && data.dataset) || {};
  const rules = suggestions.length;
  const teamCount = all.length - personRules.length;
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
  const rulesLabel = personLevel
    ? `<span class="stat-inline"><b>${rules}</b> person rule${rules === 1 ? '' : 's'}${
      teamCount ? ` <span class="muted">(${teamCount} team)</span>` : ''}</span>`
    : `<span class="stat-inline"><b>${rules}</b> rule${rules === 1 ? '' : 's'}</span>`;
  return `${rulesLabel}
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
  const personRules = (data.suggestions || []).filter(isPersonRule);
  if (personRules.length) return '';
  return 'No person-level rule crosses its threshold in this window' +
    (below ? '; the team tab names the team-level rules.' : '.');
}

export function rulesMarkup(data, {
  base = '', prSize = null, caps = null, verdict = null, showAllRules = false,
} = {}) {
  const all = (data && data.suggestions) || [];
  // prd/63 F28: the `people` toggle used to relabel the same team list. It now
  // filters: a person-level card shows only person-scoped rules, and when
  // there is none it says so (with the team count) instead of repeating them.
  const personLevel = Boolean(data && data.level === 'person');
  const personRules = all.filter(isPersonRule);
  const teamRules = all.filter((suggestion) => !isPersonRule(suggestion));
  const suggestions = personLevel ? personRules : all;
  const note = personSignalNote(data, { below: teamRules.length > 0 });
  const lab = labBestLine(data);
  // prd/58: the same verdict as Overview's card, as a one-line banner, and the
  // ladder's progressive disclosure. The current rung's levers stay expanded;
  // every other lever sits behind "Rules for the next constraint". `measured`
  // rules are diagnostics and never hidden; ranks and at_stake are untouched.
  const banner = verdict ? verdictBanner(verdict) : '';
  const rung = currentRung(verdict);
  const rungLabel = (verdict && verdict.rule_rungs && verdict.rule_rungs.labels
    && verdict.rule_rungs.labels[rung]) || null;
  const disclosable = Boolean(verdict && verdict.available && rung);
  const split = disclosable && !showAllRules
    ? {
      shown: suggestions.filter((suggestion) => isDisclosedByDefault(suggestion, verdict)),
      hidden: suggestions.filter((suggestion) => !isDisclosedByDefault(suggestion, verdict)),
    }
    : { shown: suggestions, hidden: [] };
  const measuredCount = split.shown.filter(
    (suggestion) => ruleRung(suggestion.id, verdict) === 'measured',
  ).length;
  const disclosureBar = disclosable
    ? `<div class="rules-disclosure-bar" data-rules-disclosure>
        <span class="muted">${showAllRules
          ? `showing all ${suggestions.length} rules — ranked by impact × confidence, exactly as before`
          : `showing the ${esc(rungLabel || rung)} rung${measuredCount > 0 ? ` and ${measuredCount} measured diagnostic${measuredCount === 1 ? '' : 's'}` : ''}; ${split.hidden.length} lever${split.hidden.length === 1 ? '' : 's'} behind the next constraint`}</span>
        <button type="button" class="chip" data-rule-show-all
          aria-pressed="${showAllRules ? 'true' : 'false'}"
          title="no rank and no at-stake number changes — only the default disclosure">${
          showAllRules ? 'Show the current rung only' : 'Show all rules'}</button>
      </div>`
    : '';
  const teamLink = personLevel && teamRules.length && !suggestions.length
    ? ` <button type="button" class="link" data-rule-level="team">show the ${teamRules.length} team rule${teamRules.length === 1 ? '' : 's'} →</button>`
    : '';
  // A person-level list is a filter, not the whole page: say where the team
  // rules went (the toggle is the other tab).
  const teamHint = personLevel && suggestions.length && teamRules.length
    ? `<p class="rules-note" data-team-hint>Showing the ${suggestions.length} person-level rule${
      suggestions.length === 1 ? '' : 's'}; the ${teamRules.length} team rule${
      teamRules.length === 1 ? ' is' : 's are'} on the team tab. ${
      `<button type="button" class="link" data-rule-level="team">show them →</button>`
    }</p>`
    : '';
  if (!suggestions.length) {
    // prd/64 F-05: `note` already says "no person-level rule crosses its
    // threshold in this window"; printing the same sentence again below it made
    // the empty state read twice. One statement, once.
    const emptyLine = personLevel
      ? 'No person-level rule crosses its threshold in this window.'
      : 'No rule crosses its threshold in this window.';
    return `${banner}<div class="rules-empty">
      <p data-rules-empty>${note ? esc(note) : emptyLine}${teamLink}</p>
      ${lab ? `<p class="rules-lab" data-lab-best>${esc(lab)}</p>` : ''}
      <p class="muted">${personLevel
        ? `Person rules name a pseudonym and a per-person threshold; ${teamRules.length} team rule${teamRules.length === 1 ? ' is' : 's are'} on the team tab.`
        : 'That is a result, not a missing screen: first review under 24 h, cycle time under 9 d (72 business hours), two review rounds or fewer, no lost approvals. Widen the timeframe or switch to people to check the per-person rules.'}</p>
    </div>
    ${firstPassPanels(data && data.metrics, { base })}
    ${worklogPanel((data && data.metrics && data.metrics.worklog_cross_check) || null)}
    ${prSizePanel(prSize, { caps })}
    ${reworkPanel((data && data.rework) || null, { base })}`;
  }
  // prd/39: the calibration panel sits next to the size rule (or after the list
  // when the rule does not fire), so the threshold the rule quotes is adjustable
  // exactly where it is read.
  const rows = split.shown.map((suggestion) =>
    ruleRow(suggestion, { base, metrics: (data && data.metrics) || {} }));
  const hiddenRows = split.hidden.map((suggestion) =>
    ruleRow(suggestion, { base, metrics: (data && data.metrics) || {} }));
  const panel = prSizePanel(prSize, { caps });
  if (panel) {
    // prd/58: the panel follows the *shown* rows; the size rule may be behind
    // the next-constraint disclosure.
    const index = split.shown.findIndex((suggestion) => suggestion.metric === 'pr_size');
    if (index >= 0) rows.splice(index + 1, 0, panel);
    else rows.push(panel);
  }
  // prd/53 §1/§3: the worklog card carries its chart (logged vs points, logged
  // vs active) and the per-week trend. It rides the card when the rule fires,
  // and answers "can we trust the logs?" at the end of the list when it does
  // not (a trustworthy worklog is a result, not a missing panel).
  const worklog = worklogPanel((data && data.metrics && data.metrics.worklog_cross_check) || null);
  if (worklog) {
    const index = split.shown.findIndex((suggestion) => suggestion.metric === 'worklog_cross_check');
    if (index >= 0) rows.splice(index + 1, 0, worklog);
    else rows.push(worklog);
  }
  return `${banner}${note ? `<p class="rules-note">${esc(note)}${teamLink}</p>` : ''}
    ${teamHint}
    ${lab ? `<p class="rules-lab" data-lab-best>${esc(lab)}</p>` : ''}
    ${disclosureBar}
    ${firstPassPanels(data && data.metrics, { base })}
    <div class="rules-list">${rows.join('')}</div>
    ${split.hidden.length ? `<details class="rules-next" data-next-constraint-rules>
      <summary>Rules for the next constraint (${split.hidden.length})</summary>
      <p class="panel-note">These levers belong to a lower-ranked constraint. Acting
        on the rung above is the order; opening this list changes no rank and no
        at-stake number.</p>
      <div class="rules-list">${hiddenRows.join('')}</div>
    </details>` : ''}
    ${reworkPanel((data && data.rework) || null, { base })}`;
}

export function rulesMarkdown(data, base = '') {
  if (!data) return '';
  const all = data.suggestions || [];
  const personLevel = data.level === 'person';
  const personRules = all.filter(isPersonRule);
  // prd/63 F28: the export follows the toggle. On the people tab it lists the
  // person rules only; the team rules are a heading and a count, not a repeat.
  const listed = personLevel ? personRules : all;
  const note = personLevel && !personRules.length
    ? `No person-level rule crosses its threshold in this window; ` +
      `${all.length} team rule${all.length === 1 ? ' is' : 's are'} on the team tab.`
    : '';
  // B16: the heading must not claim "(person)" over team rules.
  const level = personLevel
    ? (personRules.length ? 'person' : 'people — none')
    : (data.level || 'team');
  const dataset = data.dataset || {};
  const outside = dataset.prs_outside_window || 0;
  const prs = Number(data.sample.prs || 0);
  const days = Number(data.sample.days || 0);
  // prd/63 F28: "7 PRs opened before the window and is not in the cohort" mixed
  // the cohort and the excluded set in one singular clause. Both are counts of
  // PRs, so both pluralise.
  const cohortLine = `${prs} PR${prs === 1 ? '' : 's'} opened over ` +
    `${days} calendar day${days === 1 ? '' : 's'}` +
    (outside
      ? ` of a ${dataset.days}-day dataset — ${outside} PR${outside === 1 ? '' : 's'} ` +
        `opened before the window ${outside === 1 ? 'is' : 'are'} not in the cohort`
      : '');
  const lines = [
    `# Rules — what should we change first? (${level})`,
    '',
    `_${cohortLine} · ` +
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
  // prd/62: the cost side travels with the export too — the step table, the
  // stability rates and the labelled proxy.
  const firstPass = yieldMarkdown(data.metrics || {}, base);
  if (firstPass.length) lines.push(...firstPass, '');
  for (const suggestion of listed) {
    const facts = evidenceItems(suggestion.evidence)
      .map((fact) => `${fact.label} ${markdownLinkTickets(fact.value, base)}`).join(' · ');
    const shared = suggestion.at_stake && suggestion.at_stake.attributed_to
      && suggestion.at_stake.attributed_to !== suggestion.id
      && Number(suggestion.at_stake.pool_rules) >= 2
      ? ` — counted in #${suggestion.at_stake.attributed_to_rank}` : '';
    lines.push(
      `## ${suggestion.rank}. ${suggestion.title}`,
      '',
      `- severity: ${suggestion.severity}`,
      `- metric: ${metricLabel(suggestion.metric)}`,
      `- impact: ${fmtImpact(suggestion.impact_hours)} (confidence ${suggestion.confidence})${shared}${
        (suggestion.evidence || {}).impact_assumed ? ' — assumed, not measured' : ''}`,
      `- rule: ${suggestion.suggestion}`,
      ...((suggestion.evidence || {}).relationship === 'correlation'
        ? ['- relationship: correlation — the two numbers move together; that is not proof that one causes the other'] : []),
      ...(suggestion.policy_lab && suggestion.policy_lab.note
        ? [`- policy lab${suggestion.policy_lab.best ? ' (best scenario)' : ''}: ${suggestion.policy_lab.note}`] : []),
      ...(suggestion.policy_lab && suggestion.policy_lab.guardrail
        ? [`- guardrail: ${suggestion.policy_lab.guardrail.text}`] : []),
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
  // prd/53: the worklog block is part of the export, not only the screen.
  const worklog = worklogMarkdown(
    (data.metrics && data.metrics.worklog_cross_check) || null,
  );
  if (worklog.length) lines.push(...worklog, '');
  // prd/51: the story-level rework panel travels with the export too.
  const rework = reworkMarkdown(data.rework || null, base);
  if (rework.length) lines.push(...rework, '');
  lines.push(`_${data.note || ''}_`);
  return lines.join('\n');
}

/* Story-level rework (prd/51): the aggregate the PR-level rule cannot show.
 *
 * `/api/insights.rework` is one payload of four signal groups derived from the
 * story status history and the PR status vectors:
 *
 *   - reopened            done → ready (the story came back after Done)
 *   - failed_verification in_verification → in_progress (QA sent it back)
 *   - review_bounces      in_review → in_progress (review sent it back)
 *   - approved_unmerged   every reviewer approved, the PR never merged
 *
 * The panel is pure markup so it can be tested without a browser: each row is
 * a `#ticket/KEY` link with its count and hours, and each group states its own
 * count and cost. Nothing is invented here — `basis` comes from the payload.
 */

import { fmtHours, esc } from './format.js';
import { ticketHref, ticketLink } from './tickets.js';

export const REWORK_GROUPS = [
  {
    key: 'reopened',
    label: 'Reopened after done',
    hint: 'done → ready: the story came back after it was closed',
    unit: 'reopen',
  },
  {
    key: 'failed_verification',
    label: 'Failed verification',
    hint: 'in verification → in progress: QA sent it back',
    unit: 'loop',
  },
  {
    key: 'review_bounces',
    label: 'Review bounces',
    hint: 'in review → in progress: review sent it back',
    unit: 'loop',
  },
  {
    key: 'approved_unmerged',
    label: 'Approved but never merged',
    hint: 'every reviewer approved and the PR never merged',
    unit: 'PR',
  },
];

function rowsOf(group) {
  return (group && group.rows) || [];
}

function plural(count, one, many = `${one}s`) {
  return `${count} ${count === 1 ? one : many}`;
}

function detailCell(key, row) {
  if (key === 'approved_unmerged') {
    const approved = (row.approved_by || []).join(' + ') || 'nobody';
    const state = row.closed_by
      ? `${row.bot_closed ? 'closed by the bot ' : 'closed by '}${row.closed_by}`
      : 'still open';
    return `approved by ${approved} · ${state}`;
  }
  if (key === 'reopened') {
    return row.open
      ? 'still open after the reopen'
      : 'closed again after the reopen';
  }
  const last = (row.events || [])[(row.events || []).length - 1] || {};
  return last.recovered
    ? `returned to the stage ${fmtSince(last.recovered)}`
    : 'not recovered yet';
}

function fmtSince(iso) {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return '';
  return new Date(ms).toISOString().slice(0, 10);
}

function countCell(key, row) {
  if (key === 'approved_unmerged') {
    return `#${esc(row.number)} ${esc(row.repo_short || '')}`.trim();
  }
  return `×${Number(row.count) || 1}`;
}

/**
 * The story-level rework panel, or '' when the store has no ticket layer.
 *
 * Every group is rendered even at zero: "no failed verification in this store"
 * is a result, and a missing group would read as an unmeasured one. Rows are
 * `<tr data-rework-row>` so a test (and the QA harness) can count them without
 * parsing prose.
 */
export function reworkPanel(rework, { base = '' } = {}) {
  if (!rework || !rework.available) return '';
  const groups = REWORK_GROUPS.map(({ key, label, hint, unit }) => {
    const group = rework[key] || {};
    const rows = rowsOf(group);
    const count = Number(group.count) || 0;
    const hours = Number(group.hours) || 0;
    const body = rows.length
      ? `<div class="table-wrap"><table class="table compact rework-table"><thead><tr>
          <th class="c-id">ticket${unit === 'PR' ? ' / PR' : ''}</th>
          <th class="c-note">detail</th>
          <th class="c-dur num">${unit === 'PR' ? 'pr' : 'count'}</th>
          <th class="c-dur num">hours</th>
        </tr></thead><tbody>${rows.map((row) => `<tr
            data-rework-row="${esc(row.key)}" data-rework-group="${esc(key)}"
            data-rework-count="${Number(row.count) || 1}">
          <td class="c-id" data-label="ticket">${ticketLink(row.key, { base })}</td>
          <td class="c-note" data-label="detail">${esc(detailCell(key, row))}</td>
          <td class="c-dur num" data-label="${unit === 'PR' ? 'pr' : 'count'}">${countCell(key, row)}</td>
          <td class="c-dur num" data-label="hours">${esc(fmtHours(row.hours))}</td>
        </tr>`).join('')}</tbody></table></div>`
      : '<p class="empty-row rework-empty">none in this store.</p>';
    return `<details class="rework-group" data-rework-section="${esc(key)}"${
      rows.length ? ' open' : ''}>
      <summary><b>${esc(label)}</b>
        <span class="num" data-rework-group-count="${esc(key)}">${count}</span>
        <span class="rework-hint">${esc(hint)}</span>
        <span class="num rework-hours" data-rework-group-hours="${esc(key)}">${esc(fmtHours(hours))}</span>
      </summary>
      ${body}
    </details>`;
  }).join('');
  const counts = REWORK_GROUPS.map(({ key }) => {
    const group = rework[key] || {};
    return `${Number(group.count) || 0} ${key.replace(/_/g, ' ')}`;
  });
  return `<section class="panel wide rework-panel" id="story-rework">
    <div class="panel-head"><h2>Story rework</h2>
      <span class="pill ${Number(rework.count) ? 'warning' : 'healthy'}" data-rework-total>${
        plural(Number(rework.count) || 0, 'signal')}</span>
      <span class="muted">${esc(counts.join(' · '))} · ${esc(fmtHours(rework.hours))} in all</span></div>
    <div class="rework-groups">${groups}</div>
    <p class="panel-note">${esc(rework.scope || '')}${
      rework.basis ? ` · ${esc(rework.basis)}` : ''}</p>
  </section>`;
}

/** The "Copy as Markdown" half: the same groups and rows as links. */
export function reworkMarkdown(rework, base = '') {
  if (!rework || !rework.available) return [];
  const lines = [
    `## Story rework (${Number(rework.count) || 0} signals · ${fmtHours(rework.hours)})`,
    '',
  ];
  for (const { key, label } of REWORK_GROUPS) {
    const group = rework[key] || {};
    const rows = rowsOf(group);
    if (!rows.length) {
      lines.push(`- ${label}: none`);
      continue;
    }
    lines.push(
      `- ${label} (${Number(group.count) || 0} · ${fmtHours(group.hours)}): ` +
      rows.map((row) => (key === 'approved_unmerged'
        ? `[${row.key}](${ticketHref(row.key, base)}) #${row.number} (${fmtHours(row.hours)})`
        : `[${row.key}](${ticketHref(row.key, base)}) ×${Number(row.count) || 1} (${fmtHours(row.hours)})`
      )).join(', '),
    );
  }
  if (rework.basis) lines.push('', `_${rework.basis}._`);
  return lines;
}

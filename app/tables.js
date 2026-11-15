/* Delivery list (prd/09 §5.3) and the Overview summary.
 *
 * A triage table, not a dashboard: dense single-line rows, counts before
 * content, the actionable subset is the default, absolute start + right-aligned
 * human duration, and an impact column that is blank when it is zero. Numbers
 * are mono/tabular, state is a tinted pill, and zero is never red.
 */

import { fmtAbsolute, fmtHours, esc, whoLabel } from './format.js';

export const TABS = [
  { id: 'blocked', label: 'Blocked' },
  { id: 'oldest', label: 'Oldest waiting' },
  { id: 'all', label: 'All' },
];

export function tabRows(data, tab) {
  const rows = (data && data.rows) || [];
  if (tab === 'blocked') return rows.filter((row) => row.blocked);
  if (tab === 'oldest') return rows.filter((row) => !row.blocked && row.waiting_hours >= 72)
    .sort((a, b) => b.waiting_hours - a.waiting_hours);
  return rows;
}

export function tabCounts(data) {
  if (!data) return { blocked: 0, oldest: 0, all: 0 };
  return {
    blocked: data.counts.blocked,
    oldest: data.counts.oldest_waiting,
    all: data.counts.all,
  };
}

export function pill(kind, text, title = '') {
  return `<span class="pill ${kind}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
}

/** The status column: what the item is waiting on, in words. */
export function rowState(row) {
  if (row.blocked) return pill('blocked', 'blocked', blockedReason(row));
  if (row.waiting_hours >= 7 * 24) return pill('waiting', 'waiting 1w+', 'waiting more than a week');
  if (row.phase === 'review') return pill('neutral', 'in review');
  if (row.phase === 'active') return pill('neutral', 'in progress');
  return pill('neutral', row.phase);
}

function blockedReason(row) {
  if (row.kind === 'pr_needs_work' || row.kind === 'needs_work') return 'a reviewer asked for changes';
  if (row.kind === 'pr_unapproved' || row.kind === 'unapproved') return 'an approval was withdrawn';
  if (row.kind === 'blocker_comment') return 'a blocker comment is open';
  return 'waiting on a change';
}

/** Impact is an estimate: dependents × current wait, marked with ≈. */
export function impactCell(row) {
  if (!row.dependents || !row.impact_hours) return '<span class="blank">–</span>';
  return `<span class="num">≈${esc(fmtHours(row.impact_hours))}</span>`;
}

export function deliveryTable(data, tab, { scope = null } = {}) {
  const rows = tabRows(data, tab).filter((row) => !scope || matchesScope(row, scope));
  if (!rows.length) {
    return `<div class="empty-row">Nothing in this subset${
      scope ? ` for scope ${esc(scope.label)}` : ''
    }. Counts are honest: an empty tab means no item matches.</div>`;
  }
  const head = `<thead><tr>
      <th class="c-state">Status</th>
      <th class="c-id">Ticket / PR</th>
      <th class="c-who">Waiting on</th>
      <th class="c-phase">Phase</th>
      <th class="c-since">Waiting since</th>
      <th class="c-dur num">Waiting</th>
      <th class="c-impact num" title="dependents × current wait, estimated">Impact</th>
    </tr></thead>`;
  const body = rows.map((row) => `
    <tr tabindex="0" data-id="${esc(row.object_id)}" class="row">
      <td class="c-state">${rowState(row)}</td>
      <td class="c-id"><span class="keys">${esc(row.tickets.join(' ')) || '—'}</span>
        <span class="mono id">#${esc(row.number)}</span>
        <span class="repo">${esc(row.repo_short)}</span></td>
      <td class="c-who">${esc(whoLabel(row.ball_label || row.ball))}</td>
      <td class="c-phase"><span class="mono">${esc(row.phase)}</span></td>
      <td class="c-since mono">${esc(fmtAbsolute(Date.parse(row.waiting_since)))}</td>
      <td class="c-dur num">${esc(fmtHours(row.waiting_hours))}</td>
      <td class="c-impact num">${impactCell(row)}</td>
    </tr>`).join('');
  return `<table class="table delivery">${head}<tbody>${body}</tbody></table>`;
}

export function matchesScope(row, scope) {
  if (!scope || scope.kind === 'all') return true;
  const person = scope.value;
  if (scope.kind === 'person') {
    return row.author === person || row.waiting_for === person || row.ball === person;
  }
  if (scope.kind === 'role') {
    if (scope.value === 'author') return row.author === row.waiting_for;
    if (scope.value === 'review') return row.phase === 'review' && row.author !== row.waiting_for;
    return true;
  }
  return true;
}

export function deliveryMarkdown(rows, meta) {
  const header = [
    `# Delivery list — ${rows.length} open items`,
    '',
    meta ? `_${meta}_` : '',
    '',
    '| status | ticket / PR | waiting on | since | waiting | impact |',
    '| --- | --- | --- | --- | --- | --- |',
  ];
  const body = rows.map((row) => `| ${row.blocked ? 'blocked' : row.phase} | ${row.tickets.join(' ') || '—'} #${row.number} ${row.repo_short} | ${whoLabel(row.ball_label || row.ball)} | ${fmtAbsolute(Date.parse(row.waiting_since))} | ${fmtHours(row.waiting_hours)} | ${row.dependents ? `≈${fmtHours(row.impact_hours)}` : '—'} |`);
  return [...header, ...body].join('\n');
}

/** The overview: counts first, then the rows that need attention. */
export function overviewMarkup({ delivery, queues, blocking }) {
  const counts = delivery ? delivery.counts : { open: 0, blocked: 0, oldest_waiting: 0 };
  const streams = (queues && queues.streams) || [];
  const overloaded = streams.filter((s) => s.summary.diagnosis === 'overloaded');
  const slow = streams.filter((s) => s.summary.diagnosis === 'slow');
  const topBlocking = (blocking && blocking.edges ? blocking.edges.slice(0, 3) : []);
  const waitHours = blocking && blocking.totals ? blocking.totals.hours : 0;
  const rows = delivery ? tabRows(delivery, 'blocked').concat(
    tabRows(delivery, 'oldest').filter((row) => !row.blocked),
  ).slice(0, 6) : [];
  return `
    <div class="stats">
      <div class="stat"><span class="stat-n ${counts.blocked ? 'attention' : ''}">${counts.blocked}</span>
        <span class="stat-k">blocked</span>
        <span class="stat-note">${counts.blocked ? 'waiting on a change, not on a person' : 'nothing blocked'}</span></div>
      <div class="stat"><span class="stat-n">${counts.oldest_waiting}</span>
        <span class="stat-k">waiting over 3 days</span>
        <span class="stat-note">of ${counts.open} open items</span></div>
      <div class="stat"><span class="stat-n">≈${fmtHours(waitHours).replace(' ', ' ')}</span>
        <span class="stat-k">measured wait</span>
        <span class="stat-note">wall clock, all handovers in the window</span></div>
      <div class="stat"><span class="stat-n">${overloaded.length}</span>
        <span class="stat-k">overloaded queues</span>
        <span class="stat-note">${slow.length} slow · longest mean wait ${
          streams.length ? `≈${fmtHours(streams[0].summary.mean_wait_hours)}` : '–'}</span></div>
    </div>
    <div class="overview-grid">
      <section class="panel">
        <div class="panel-head"><h2>Needs attention</h2>
          <button type="button" class="link" data-go="delivery">Delivery list →</button></div>
        ${rows.length ? `<table class="table compact"><tbody>${rows.map((row) => `
          <tr tabindex="0" data-id="${esc(row.object_id)}" class="row">
            <td class="c-state">${rowState(row)}</td>
            <td class="c-id"><span class="mono id">#${esc(row.number)}</span>
              <span class="repo">${esc(row.repo_short)}</span></td>
            <td class="c-dur num">${esc(fmtHours(row.waiting_hours))}</td>
          </tr>`).join('')}</tbody></table>`
    : '<div class="empty-row">No open item is waiting right now.</div>'}
      </section>
      <section class="panel">
        <div class="panel-head"><h2>Where waits accumulate</h2>
          <button type="button" class="link" data-go="blocking">Blocking map →</button></div>
        ${topBlocking.length ? `<table class="table compact"><tbody>${topBlocking.map((edge) => `
          <tr><td class="c-who"><span class="mono">${esc(edge.from)}</span> waited on
            <span class="mono">${esc(edge.to)}</span></td>
          <td class="c-dur num">${esc(fmtHours(edge.hours))}</td></tr>`).join('')}</tbody></table>`
    : '<div class="empty-row">No measured handover in this window.</div>'}
        <p class="panel-note">${esc((blocking && blocking.assumption) || '')}</p>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>Queue diagnosis</h2>
          <button type="button" class="link" data-go="queue">Queue view →</button></div>
        ${streams.length ? `<table class="table compact"><tbody>${streams.slice(0, 6).map((stream) => `
          <tr><td class="c-who"><span class="mono">${esc(stream.id)}</span></td>
            <td class="c-state">${pill(stream.summary.diagnosis, `≈ ${stream.summary.diagnosis}`)}</td>
            <td class="c-dur num">≈${esc(fmtHours(stream.summary.mean_wait_hours))}</td></tr>`).join('')}</tbody></table>`
    : '<div class="empty-row">No measured waits in this window.</div>'}
      </section>
    </div>`;
}

/** Detail panel body for one item (over- + name + context + impact + facts). */
export function detailMarkup(entity) {
  const pr = entity.pr;
  const row = entity.row || null;
  const overline = entity.overline || 'PULL REQUEST';
  const name = pr
    ? `${pr.keys && pr.keys[0] ? `${pr.keys[0]} ` : ''}#${pr.number} ${pr.repo}`
    : `${row ? `${row.tickets.join(' ')} ` : ''}#${row.number} ${row.repo_short}`;
  const context = pr
    ? `author ${pr.actor} · ${pr.state}${pr.closed ? '' : ' · right-censored'}`
    : `author ${row.author} · ${row.state}`;
  const facts = entity.facts || [];
  return `
    <div class="detail-head">
      <span class="overline">${esc(overline)}</span>
      <h2 class="mono">${esc(name)}</h2>
      <p class="context">${esc(context)}</p>
    </div>
    ${entity.impact ? `<p class="impact">${esc(entity.impact)}</p>` : ''}
    <dl class="facts">${facts.map(([key, value]) => `
      <dt>${esc(key)}</dt><dd class="mono">${value == null || value === '' ? '–' : esc(value)}</dd>`).join('')}
    </dl>
    ${entity.children && entity.children.length ? `
      <div class="children"><span class="overline">Related</span>
        ${entity.children.map((child) => `<span class="child">${esc(child)}</span>`).join('')}
      </div>` : ''}
    <div class="detail-actions">
      <button type="button" class="chip" data-detail="timeline">Timeline</button>
      <button type="button" class="chip" data-detail="blocking">Blocking map</button>
      <button type="button" class="chip" data-detail="copy">Copy as Markdown</button>
    </div>`;
}

/** Impact sentence: stated before the numbers (prd/10 §5.4). */
export function impactSentence(row) {
  if (!row) return '';
  const dependents = row.dependents || 0;
  if (!dependents) return `Waiting ${fmtHours(row.waiting_hours)} on ${whoLabel(row.ball_label || row.ball)}; no other open PR shares its ticket.`;
  return `When this PR waits: ${dependents} other open PR${dependents === 1 ? '' : 's'} share its ticket, ` +
    `so ≈${fmtHours(row.impact_hours)} of downstream wait accumulate behind it.`;
}

export function entityMarkdown(entity, hoursMode = 'wall-clock') {
  if (entity.row) {
    const row = entity.row;
    return [
      `## ${row.tickets.join(' ') || '—'} #${row.number} ${row.repo_short}`,
      '',
      `- status: ${row.blocked ? 'blocked' : row.phase}`,
      `- waiting on: ${whoLabel(row.ball_label || row.ball)}`,
      `- waiting since: ${fmtAbsolute(Date.parse(row.waiting_since))} (${fmtHours(row.waiting_hours)})`,
      `- age: ${fmtHours(row.age_hours)}`,
      row.dependents ? `- impact: ≈${fmtHours(row.impact_hours)} over ${row.dependents} dependent PR(s)` : '',
      '',
      '_' + (hoursMode === 'business'
        ? 'business hours (tenant working-hours calendar)'
        : 'wall-clock hours; no working-hours calendar configured') + '_',
    ].filter(Boolean).join('\n');
  }
  const pr = entity.pr;
  if (!pr) return '';
  return [
    `## ${pr.keys.join(' ') || '—'} #${pr.number} ${pr.repo}`,
    '',
    `- author: ${pr.actor}`,
    `- state: ${pr.state}`,
    `- created: ${fmtAbsolute(pr.created)}`,
  ].join('\n');
}


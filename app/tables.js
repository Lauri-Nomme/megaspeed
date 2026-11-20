/* Delivery list (prd/09 §5.3) and the Overview summary.
 *
 * A triage table, not a dashboard: dense single-line rows, counts before
 * content, the actionable subset is the default, absolute start + right-aligned
 * human duration, and an impact column that is blank when it is zero. Numbers
 * are mono/tabular, state is a tinted pill, and zero is never red.
 */

import { fmtAbsolute, fmtHours, esc, whoLabel, BUSINESS_HOURS_PER_DAY } from './format.js';
import { benchmarkInline, benchmarkText } from './benchmark.js';
import { isTicketKey, rowTicket, ticketHref, ticketLink } from './tickets.js';
import { streamIsQueue } from './scope.js';

export const TABS = [
  { id: 'blocked', label: 'Blocked' },
  { id: 'oldest', label: 'Oldest waiting' },
  { id: 'all', label: 'All' },
];

/**
 * "5 d = 40 h" — the one waiting-week threshold, from the payload (prd/41 B3).
 *
 * concepts.md §1: a working week is 5 d = 40 business hours. Every label that
 * says "over a week" reads this, so the card, the header, the pill and the
 * footer cannot drift apart.
 */
export function weekLabel(data) {
  const hours = Number((data && data.counts && data.counts.waiting_week_hours) || 40);
  const days = Number((data && data.counts && data.counts.waiting_week_days) ||
    hours / BUSINESS_HOURS_PER_DAY);
  return `${days} d = ${hours} h`;
}

export function tabRows(data, tab) {
  const rows = (data && data.rows) || [];
  // prd/41 B3: one threshold — a working week is 40 business hours
  // (concepts.md §1) — and the payload states it so no view invents its own.
  const week = Number((data && data.counts && data.counts.waiting_week_hours) || 40);
  if (tab === 'blocked') return rows.filter((row) => row.blocked);
  if (tab === 'oldest') return rows.filter((row) => !row.blocked && row.waiting_hours >= week)
    .sort((a, b) => b.waiting_hours - a.waiting_hours);
  return rows;
}

/**
 * Who a row waits on, in words (prd/41 B6).
 *
 * When several people hold the ball the API keeps the role label (`review`) but
 * names every holder in `waiting_on`; naming them beats "review queue" — #53's
 * "waiting on CaOs" hid that the sentinel owed a verdict too.
 */
export function waitingLabel(row) {
  if (!row) return '';
  const label = row.ball_label || row.ball;
  const people = (row.waiting_on || []).filter((person) => person && person !== row.author);
  if (label === 'review' && people.length) return people.join(' + ');
  return whoLabel(label);
}

/**
 * prd/40: the one-line state of a PR, from the server's status vector.
 *
 * "needs rework by X · 2/3 reviewers pending · ready to merge since T" — the
 * three facts the old single `ball` label could not state together. Each part
 * is optional; a ready PR says so, and a doubly blocked one names both sides.
 */
export function reviewStateLine(row) {
  if (!row) return '';
  const parts = [];
  if (row.author_open) {
    parts.push(`needs rework by ${whoLabel(row.author)}`);
  }
  const pending = Number(row.reviewers_pending || 0);
  const total = Number(row.reviewers_total || 0);
  const approved = Number(row.reviewers_approved || 0);
  // prd/41 B6: "#53 is 0/2 approved" is the fact that matters when nobody has
  // approved yet; the pending count then says both reviewers owe a verdict.
  if (row.reviewers_approved != null && total > 0) {
    parts.push(`${approved}/${total} approved`);
  }
  if (pending > 0) parts.push(`${pending}/${total} reviewers pending`);
  else if (row.ball === 'ready' && total > 0) parts.push('all reviewers answered');
  if (row.ball === 'ready' && row.ready_since) {
    parts.push(`ready to merge since ${fmtAbsolute(Date.parse(row.ready_since))}`);
  }
  return parts.join(' · ');
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
export function rowStateText(row) {
  if (row.ball === 'ready') return 'ready to merge';
  if (row.blocked) return 'blocked';
  if (row.stale) return 'stale';
  // concepts.md §1: one working week is 5 d = 40 business hours.
  if (row.waiting_hours >= 40) return 'waiting 1w+';
  if (row.phase === 'review') return 'in review';
  if (row.phase === 'active') return 'in progress';
  return row.phase;
}

export function rowState(row) {
  if (row.ball === 'ready') return pill('healthy', 'ready to merge', 'all reviewers answered; the merge click is the author\'s');
  if (row.blocked) return pill('blocked', 'blocked', blockedReason(row));
  if (row.waiting_hours >= 40) return pill('waiting', 'waiting 1w+', 'waiting more than a working week (40 business hours)');
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

export function deliveryTable(data, tab, { scope = null, benchmark = true } = {}) {
  const rows = tabRows(data, tab).filter((row) => !scope || matchesScope(row, scope));
  if (!rows.length) {
    // B11: an empty tab is a result, not a refusal. `Blocked 0` says so.
    const suffix = scope ? ` for scope ${esc(scope.label)}` : '';
    if (tab === 'blocked') {
      return `<div class="empty-row">Nothing blocked${suffix} — good. No open item is waiting on a change.</div>`;
    }
    return `<div class="empty-row">Nothing in this subset${suffix}. Counts are honest: an empty tab means no item matches.</div>`;
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
    <tr tabindex="0" data-id="${esc(row.object_id)}" data-pr="${esc(row.object_id)}"
        class="row" role="button"
        title="open PR #${esc(row.number)} ${esc(row.repo_short)} in the drawer"
        aria-label="open PR #${esc(row.number)} ${esc(row.repo_short)} in the drawer">
      <td class="c-state" data-label="status">${rowState(row)}${
        row.stale ? ` ${pill('waiting', 'stale', (row.stale_reasons || []).join('; '))}` : ''}${
        row.unobserved ? ` ${pill('neutral', 'unobserved', 'no events since the snapshot — the blocking graph excludes this tail')}` : ''}</td>
      <td class="c-id" data-label="">${ticketLink(rowTicket(row), { cls: 'keys' }) || '<span class="keys">—</span>'}
        <span class="mono id">#${esc(row.number)}</span>
        <span class="repo">${esc(row.repo_short)}</span></td>
      <td class="c-who" data-label="waiting on">${esc(waitingLabel(row))}</td>
      <td class="c-phase" data-label="phase"><span class="mono">${esc(row.phase)}</span></td>
      <td class="c-since mono" data-label="waiting since">${esc(fmtAbsolute(Date.parse(row.waiting_since)))}</td>
      <td class="c-dur num" data-label="waiting">${esc(fmtHours(row.waiting_hours))}${
        benchmarkInline(row.wait_benchmark, { enabled: benchmark })}</td>
      <td class="c-impact num" data-label="impact">${impactCell(row)}</td>
    </tr>`).join('');
  return `<table class="table delivery">${head}<tbody>${body}</tbody></table>
    <p class="panel-note">unobserved = no events since the snapshot: the blocking map excludes that tail, so the row stays here rather than disappearing. A duration is business hours on the team calendar; 1 d = 8 h.</p>`;
}

export function matchesScope(row, scope) {
  if (!scope || scope.kind === 'all') return true;
  const value = String(scope.value || '').toLowerCase();
  // prd/38 #20: a ticket scope is "the PRs that name this ticket", not the
  // author and not a person's queue.
  if (scope.kind === 'ticket') {
    const keys = [...(row.tickets || []), ...(row.story_tickets || []), rowTicket(row)]
      .filter(Boolean)
      .map((key) => String(key).toLowerCase());
    return keys.includes(value);
  }
  // concepts.md §2 + prd/34 §3: on the delivery list (and the queue view) a
  // person scope means "waiting on X" — the holders, never "authored by X".
  const waiting = new Set(
    [...(row.waiting_on || []), row.waiting_for, row.ball_label]
      .filter(Boolean)
      .map((person) => String(person).toLowerCase()),
  );
  const roles = new Set(
    Object.values(row.waiting_roles || {}).map((role) => String(role).toLowerCase()),
  );
  if (scope.kind === 'person') return waiting.has(value);
  if (scope.kind === 'role') {
    // prd/40: `ball` is the label; a ready PR is the author's merge click but
    // is nobody's review wait, so the two role scopes stay disjoint.
    if (value === 'sentinel') return roles.has('sentinel');
    if (value === 'review') {
      // a sentinel is a distinct mandatory approver, not a peer review queue;
      // with no role map (an older payload) fall back to the ball label.
      if (Object.keys(row.waiting_roles || {}).length) return roles.has('review');
      return row.ball === 'review';
    }
    if (value === 'author') return roles.has('author') || row.ball === 'ready';
    if (value === 'ready') return row.ball === 'ready';
    return String(row.ball || '').toLowerCase() === value;
  }
  return true;
}

/**
 * prd/56 §2: the people a pivot menu can scope to.
 *
 * The holder is the useful pivot ("Scope to FrMo"), not only the author
 * ("ErLi"): the row's wait is on the holder. Several holders yield several
 * entries, author last, each deduplicated. Pure, so the menu's contents are
 * testable without a DOM.
 */
export function scopeTargets(entity) {
  if (!entity) return [];
  const row = entity.row || null;
  const author = entity.author || (entity.pr && entity.pr.actor) || (row && row.author);
  const holders = row
    ? [...(row.waiting_on || []), row.ball_label]
      .filter((person) => person && person !== row.ball && person !== author)
    : [];
  const people = [...new Set(holders)];
  const targets = people.map((person) => ({ person, label: `Scope to ${person}`, holder: true }));
  if (author) targets.push({ person: author, label: `Scope to ${author}`, holder: false });
  return targets;
}

/** The delivery list as Markdown: the same rows and wording as the table. */
export function deliveryMarkdown(rows, meta, base = '') {
  const header = [
    `# Delivery list — ${rows.length} open item${rows.length === 1 ? '' : 's'}`,
    '',
    meta ? `_${meta}_` : '',
    '',
    '| status | ticket / PR | waiting on | since | waiting | impact |',
    '| --- | --- | --- | --- | --- | --- |',
  ];
  const body = rows.map((row) => {
    const key = rowTicket(row);
    const ticket = key ? `[${key}](${ticketHref(key, base)})` : '—';
    return `| ${rowStateText(row)} | ${ticket} #${row.number} ${row.repo_short} | ${waitingLabel(row)} | ${fmtAbsolute(Date.parse(row.waiting_since))} | ${fmtHours(row.waiting_hours)}${benchmarkText(row.wait_benchmark) ? ` (${benchmarkText(row.wait_benchmark)})` : ''} | ${row.dependents ? `≈${fmtHours(row.impact_hours)}` : '—'} |`;
  });
  return [...header, ...body].join('\n');
}

/**
 * prd/42 M4: "Done in Jira, PR open" as a first-class list.
 *
 * The QA run could only find these three items in the raw store. The list
 * names the ticket, the PR and who owes the next action, and calls out the
 * stronger case: a PR opened *after* its ticket was closed.
 */
export function doneOpenMarkup(delivery, { base = '', scope = null } = {}) {
  const done = delivery && delivery.done_open;
  let rows = (done && done.rows) || [];
  if (scope && scope.kind !== 'all') {
    const value = String(scope.value || '').toLowerCase();
    rows = rows.filter((row) => (scope.kind === 'ticket'
      ? String(row.key || '').toLowerCase() === value
      : (row.waiting_on || []).some((person) => String(person).toLowerCase() === value)));
  }
  if (!done || !done.available) return '';
  if (!rows.length) {
    return `<section class="panel wide done-open-panel">
      <div class="panel-head"><h2>Done in Jira, PR open</h2>
        <span class="pill healthy">none</span></div>
      <p class="panel-note">Every ticket marked Done in Jira has its code merged.</p>
    </section>`;
  }
  const body = rows.map((row) => `<tr data-done-open="${esc(row.key)}">
      <td class="c-id" data-label="ticket">${ticketLink(row.key, { base })}
        <span class="mono id">#${esc(row.number)}</span>
        <span class="repo">${esc(row.repo_short)}</span></td>
      <td class="c-who" data-label="owes">${esc((row.waiting_on || []).join(' + ') || '—')}</td>
      <td class="c-since mono" data-label="ticket closed">${esc(row.ticket_closed ? fmtAbsolute(Date.parse(row.ticket_closed)) : '—')}</td>
      <td class="c-state" data-label="note">${
        row.opened_after_close
          ? `<span class="pill waiting" title="the PR was opened after Jira closed the ticket — the work cannot have been part of the delivered story">PR opened after the ticket closed${row.opened_after_close_hours ? ` (${esc(fmtHours(row.opened_after_close_hours))} later)` : ''}</span>`
          : '<span class="pill neutral" title="Jira says Done while the code is still unmerged">unmerged</span>'}</td>
    </tr>`).join('');
  return `<section class="panel wide done-open-panel" id="done-open">
    <div class="panel-head"><h2>Done in Jira, PR open</h2>
      <span class="pill blocked">${done.count}</span>
      <span class="muted">${done.opened_after_close
        ? `${done.opened_after_close} PR${done.opened_after_close === 1 ? ' was' : 's were'} opened after the ticket closed`
        : 'the code is unmerged'}</span></div>
    <table class="table compact"><thead><tr>
      <th class="c-id">ticket / PR</th><th class="c-who">who owes</th>
      <th class="c-since">ticket closed</th><th class="c-state">note</th>
    </tr></thead><tbody>${body}</tbody></table>
    <p class="panel-note">${esc(done.note || '')}</p>
  </section>`;
}

/** prd/42 P11: a tooltip for the two jargon labels the QA run flagged. */
export function jargonTitle(term) {
  const glossary = {
    unobserved: 'no events since the snapshot — the tail is not measured, so the blocking graph excludes it',
    ready: 'every reviewer has answered and only the merge click remains — nobody is reviewing',
    'sentinel waited on ready': 'the PRs were ready to merge and waited for the merge click, not for a reviewer',
    sentinel: 'the mandatory approver on (typically) every PR — a role, not a bot',
  };
  return glossary[String(term || '').toLowerCase()] || '';
}

/** The overview: counts first, then the rows that need attention. */
export function overviewMarkup({ delivery, queues, blocking, queueLevel = 'queue', scope = null }) {
  const scoped = Boolean(scope && scope.kind !== 'all');
  const allRows = (delivery && delivery.rows) || [];
  // prd/56 §4: a person/role/ticket scope really does narrow this view, so the
  // tiles and the attention list are derived from the scoped rows — not from
  // the whole team while the banner claims the opposite.
  const rowsForScope = scoped ? allRows.filter((row) => matchesScope(row, scope)) : allRows;
  const week = Number((delivery && delivery.counts && delivery.counts.waiting_week_hours) || 40);
  const counts = delivery ? {
    open: rowsForScope.length,
    blocked: rowsForScope.filter((row) => row.blocked).length,
    oldest_waiting: rowsForScope.filter((row) => !row.blocked && row.waiting_hours >= week).length,
    stale: rowsForScope.filter((row) => row.stale).length,
    all: rowsForScope.length,
  } : { open: 0, blocked: 0, oldest_waiting: 0 };
  const streams = (queues && queues.streams) || [];
  // B17: the API response says which level it answered at; never call people
  // "queues" because another page happened to leave that level selected.
  const level = (queues && queues.level) || queueLevel || 'queue';
  const people = level === 'person';
  const noun = people ? 'people' : 'queues';
  const one = people ? 'person' : 'queue';
  // prd/34 §4: a tile that counts people counts only people. A synthetic role
  // stream (`review`, `author`, …) is a queue even when the API level says
  // "person", so it must not inflate "N overloaded people" (QA #10).
  const counted = people ? streams.filter((stream) => !streamIsQueue(stream)) : streams;
  // prd/56 §4: the queue diagnosis follows the scope too (one person, or one
  // role queue), instead of showing the whole team under a scoped banner.
  const scopedStreams = scoped
    ? counted.filter((stream) => String(stream.id) === String(scope.value))
    : counted;
  const overloaded = scopedStreams.filter((s) => s.summary.diagnosis === 'overloaded');
  const slow = scopedStreams.filter((s) => s.summary.diagnosis === 'slow');
  const allBlocking = (blocking && blocking.edges ? blocking.edges : []);
  const scopeValue = scoped ? String(scope.value || '').toLowerCase() : '';
  const topBlocking = (scoped && scope.kind !== 'ticket'
    ? allBlocking.filter((edge) => String(edge.from || '').toLowerCase() === scopeValue
      || String(edge.to || '').toLowerCase() === scopeValue)
    : allBlocking).slice(0, 3);
  const waitHours = blocking && blocking.totals ? blocking.totals.hours : 0;
  // prd/38 #25: the list is the actionable subset, not the whole table, so when
  // it is truncated say how many are left instead of silently showing six.
  const attention = delivery ? tabRows(delivery, 'blocked').filter(
    (row) => !scoped || matchesScope(row, scope),
  ).concat(
    tabRows(delivery, 'oldest').filter(
      (row) => !row.blocked && (!scoped || matchesScope(row, scope)),
    ),
  ) : [];
  const rows = attention.slice(0, 6);
  const hidden = Math.max(0, attention.length - rows.length);
  const overloadedLabel = overloaded.length === 1 ? `overloaded ${one}` : `overloaded ${noun}`;
  // prd/43 §4: "longest mean wait" is the longest *person's* mean wait, not the
  // first stream's. The synthetic `review` stream used to head the list and the
  // tile read 22.6 h while every person below it waited ≤ 1 d 3 h.
  const longest = scopedStreams.reduce(
    (best, stream) => Math.max(best, Number(stream.summary.mean_wait_hours) || 0), 0,
  );
  return `
    <div class="stats">
      <div class="stat"><span class="stat-n ${counts.blocked ? 'attention' : ''}">${counts.blocked}</span>
        <span class="stat-k">blocked</span>
        <span class="stat-note">${counts.blocked ? 'waiting on a change, not on a person' : 'nothing blocked'}</span></div>
      <div class="stat"><span class="stat-n">${counts.oldest_waiting}</span>
        <span class="stat-k">waiting over a week</span>
        <span class="stat-note">${weekLabel(delivery)} · of ${counts.open} open item${counts.open === 1 ? '' : 's'}${
          counts.stale ? ` · ${counts.stale} stale` : ''}</span></div>
      <div class="stat"><span class="stat-n">≈${fmtHours(waitHours).replace(' ', ' ')}</span>
        <span class="stat-k">measured wait</span>
        <span class="stat-note">business hours on the team calendar, all handovers in the window</span></div>
      <div class="stat"><span class="stat-n">${overloaded.length}</span>
        <span class="stat-k">${esc(overloadedLabel)}</span>
        <span class="stat-note">${slow.length} slow · longest mean wait ${
          counted.length ? `≈${fmtHours(longest)}` : '–'}</span></div>
    </div>
    <div class="overview-grid">
      <section class="panel">
        <div class="panel-head"><h2>Needs attention</h2>
          <button type="button" class="link" data-go="delivery">Delivery list →</button></div>
        ${rows.length ? `<table class="table compact"><tbody>${rows.map((row) => `
          <tr tabindex="0" data-id="${esc(row.object_id)}" data-pr="${esc(row.object_id)}"
              class="row" role="button"
              title="open PR #${esc(row.number)} ${esc(row.repo_short)} in the drawer"
              aria-label="open PR #${esc(row.number)} ${esc(row.repo_short)} in the drawer">
            <td class="c-state" data-label="status">${rowState(row)}</td>
            <td class="c-id" data-label="">${ticketLink(rowTicket(row))}
              <span class="mono id">#${esc(row.number)}</span>
              <span class="repo">${esc(row.repo_short)}</span></td>
            <td class="c-dur num" data-label="waiting">${esc(fmtHours(row.waiting_hours))}</td>
          </tr>`).join('')}</tbody></table>${
            hidden > 0
              ? `<p class="panel-note attention-more"><button type="button" class="link"
                   data-go="delivery">+${hidden} more open item${hidden === 1 ? '' : 's'} →</button>
                   <span class="muted">the full triage list</span></p>`
              : ''}`
    : '<div class="empty-row">No open item is waiting right now.</div>'}
      </section>
      <section class="panel">
        <div class="panel-head"><h2>Where waits accumulate</h2>
          <button type="button" class="link" data-go="blocking">Blocking map →</button></div>
        ${topBlocking.length ? `<table class="table compact"><tbody>${topBlocking.map((edge) => `
          <tr title="${esc(edge.to === 'ready' ? jargonTitle('sentinel waited on ready') : '')}"><td class="c-who" data-label="handover">${
            edge.to === 'ready'
              ? '<span>PRs waited for merge after approval</span>'
              : `<span class="mono">${esc(edge.from)}</span> waited on <span class="mono">${esc(edge.to)}</span>`
          }</td>
          <td class="c-dur num" data-label="wait">${esc(fmtHours(edge.hours))}</td></tr>`).join('')}</tbody></table>`
    : '<div class="empty-row">No measured handover in this window.</div>'}
        <p class="panel-note">${esc((blocking && blocking.assumption) || '')}</p>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>${people ? 'People diagnosis' : 'Queue diagnosis'}</h2>
          <button type="button" class="link" data-go="queue">Queue view →</button></div>
        ${scopedStreams.length ? `<table class="table compact"><tbody>${scopedStreams.slice(0, 8).map((stream) => `
          <tr><td class="c-who" data-label=""><span class="mono">${esc(stream.id)}</span></td>
            <td class="c-state" data-label="diagnosis">${pill(stream.summary.diagnosis, `≈ ${stream.summary.diagnosis}`, 'queue wait (person-hours from added as reviewer → answer, the peer phase included) relative to the median — not calendar utilisation and not the sentinel gate wait')}</td>
            <td class="c-dur num" data-label="mean wait">≈${esc(fmtHours(stream.summary.mean_wait_hours))}</td></tr>`).join('')}</tbody></table>${
          scopedStreams.length > 8 ? `<p class="panel-note">showing the 8 longest of ${scopedStreams.length}</p>` : ''}${
          people ? '<p class="panel-note">mean wait here is person-hours from “added as reviewer” to the answer — a different metric from the sentinel wait (Queue → response time: last peer answer → first response).</p>' : ''}`
    : `<div class="empty-row">No measured waits${scoped ? ` for scope ${esc(scope.label)}` : ''}.</div>`}
      </section>
    </div>
    ${doneOpenMarkup(delivery, { scope })}`;
}

/** Detail panel body for one item (over- + name + context + impact + facts). */
export function detailMarkup(entity, { base = '', benchmark = true } = {}) {
  const pr = entity.pr;
  const row = entity.row || null;
  const overline = entity.overline || 'PULL REQUEST';
  const key = pr ? (pr.ticket || '') : rowTicket(row);
  const rest = pr ? `#${pr.number} ${pr.repo}` : `#${row.number} ${row.repo_short}`;
  // The heading is the doorway: one anchor whose text is the whole name, so a
  // copied heading still reads "DEL-197 #52 repo" and the click opens the
  // ticket page (prd/25).
  const nameHtml = key
    ? ticketLink(key, { base, label: `${key} ${rest}` })
    : esc(rest);
  const context = pr
    ? `author ${pr.actor} · ${pr.state}${pr.closed ? '' : ' · right-censored'}`
    : `author ${row.author} · ${row.state}`;
  const facts = entity.facts || [];
  const factValue = (value) => {
    if (value == null || value === '') return '–';
    return isTicketKey(value) ? ticketLink(value, { base }) : esc(value);
  };
  return `
    <div class="detail-head">
      <span class="overline">${esc(overline)}</span>
      <h2 class="mono">${nameHtml}</h2>
      <p class="context">${esc(context)}</p>
    </div>
    ${entity.impact ? `<p class="impact">${esc(entity.impact)}</p>` : ''}
    ${entity.clockNote ? `<p class="clock-note">${esc(entity.clockNote)}</p>` : ''}
    <dl class="facts">${facts.map(([factKey, value, contrast]) => `
      <dt>${esc(factKey)}</dt><dd class="mono">${factValue(value)}${
        benchmarkInline(contrast, { enabled: benchmark })}</dd>`).join('')}
    </dl>
    ${entity.children && entity.children.length ? `
      <div class="children"><span class="overline">Related</span>
        ${entity.children.map((child) => (isTicketKey(child)
          ? ticketLink(child, { base, cls: 'child ticket-link' })
          : `<span class="child">${esc(child)}</span>`)).join('')}
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
  const ticket = rowTicket(row);
  const dependents = row.dependents || 0;
  if (!dependents) {
    return `Waiting ${fmtHours(row.waiting_hours)} on ${waitingLabel(row)}; ` +
      `no other open PR shares ${ticket ? `ticket ${ticket}` : 'its ticket'}.`;
  }
  const plural = dependents === 1 ? '' : 's';
  const verb = dependents === 1 ? 'shares' : 'share';
  return `When this PR waits: ${dependents} other open PR${plural} ${verb} ` +
    `${ticket ? `ticket ${ticket}` : 'its ticket'}, ` +
    `so ≈${fmtHours(row.impact_hours)} of downstream wait accumulate behind it.`;
}

export function entityMarkdown(entity, hoursMode = 'wall-clock', base = '', calendarId = '') {
  if (entity.row) {
    const row = entity.row;
    const key = rowTicket(row);
    const heading = key
      ? `[${key}](${ticketHref(key, base)}) #${row.number} ${row.repo_short}`
      : `— #${row.number} ${row.repo_short}`;
    return [
      `## ${heading}`,
      '',
      `- status: ${row.blocked ? 'blocked' : row.phase}`,
      `- waiting on: ${waitingLabel(row)}`,
      `- waiting since: ${fmtAbsolute(Date.parse(row.waiting_since))} (${fmtHours(row.waiting_hours)})`,
      `- age: ${fmtHours(row.age_hours)}`,
      row.epic ? `- epic: ${row.epic}` : '',
      row.dependents ? `- impact: ≈${fmtHours(row.impact_hours)} over ${row.dependents} dependent PR${row.dependents === 1 ? '' : 's'}` : '',
      '',
      '_' + (hoursMode === 'business'
        ? 'business hours (tenant working-hours calendar)'
        : 'wall-clock hours; no working-hours calendar configured') +
        (calendarId ? ` · calendar ${calendarId}` : '') + '_',
    ].filter(Boolean).join('\n');
  }
  const pr = entity.pr;
  if (!pr) return '';
  const key = pr.ticket || '';
  const heading = key
    ? `[${key}](${ticketHref(key, base)}) #${pr.number} ${pr.repo}`
    : `— #${pr.number} ${pr.repo}`;
  return [
    `## ${heading}`,
    '',
    `- author: ${pr.actor}`,
    `- state: ${pr.state}`,
    pr.epic ? `- epic: ${pr.epic}` : '',
    `- created: ${fmtAbsolute(pr.created)}`,
  ].filter(Boolean).join('\n');
}


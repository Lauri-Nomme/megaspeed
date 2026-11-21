/* The constraint verdict (prd/58): one decisive answer, not a menu.
 *
 * Overview used to name a different bottleneck on every card, and Rules
 * recommended four levers at once. This module renders the API's verdict as
 * **one card**: the station with the largest *removable* wait pool on one
 * canonical clock, the numbers behind it, and Goldratt's ladder
 * (exploit → subordinate → elevate) with the policy lab's verdict on each rung.
 * Elevate stays **blocked** while exploit items are owed, and lower-ranked rules
 * hide behind "next constraint" so the page has one answer.
 *
 * Honesty rules (concepts.md, prd/10):
 *   · one clock per concept — the pool is the story clock, the holder row is
 *     the PR ball clock, and the card says which and why;
 *   · every number names its `n` and the snapshot it is as-of;
 *   · colour means state only: the constraint pill is a warning tint, a blocked
 *     elevation is a neutral outline (a decision, never a fire);
 *   · a pool under one waiting week is "no constraint worth naming", never a
 *     manufactured headline;
 *   · no chart, no horizontal scroll, and a 44 px ladder on a phone.
 */

import { esc, fmtAbsolute, fmtHours, BUSINESS_HOURS_PER_DAY } from './format.js';
import { ticketLink } from './tickets.js';

export const OVERLINE = 'THE CONSTRAINT';
export const WEEK_HOURS = 5 * BUSINESS_HOURS_PER_DAY;

/** A pool-sized number: `619 h`, `≈1.4k h` — rounded, never a fake decimal. */
export function poolHours(hours) {
  const value = Number(hours) || 0;
  if (value >= 1000) {
    return `≈${(value / 1000).toFixed(1).replace(/\.0$/, '')}k h`;
  }
  return `${Math.round(value)} h`;
}

/** `238 h on 4 stories` — a number and its cohort, never a bare total. */
export function owedLabel(constraint) {
  const n = Number((constraint && constraint.open_n) || 0);
  return `${poolHours(constraint && constraint.live_hours)} on ${n} stor${n === 1 ? 'y' : 'ies'}`;
}

export function stationLabel(constraint) {
  const label = (constraint && constraint.label) || 'no single station';
  return label.toLowerCase() === 'approval gate' ? 'the approval gate' : label.toLowerCase();
}

/** The oldest live item behind the constraint: the one sentence names it. */
export function oldestItem(verdict) {
  const items = ((verdict && verdict.constraint && verdict.constraint.items) || [])
    .filter((item) => item && item.hours != null)
    .slice()
    .sort((a, b) => Number(b.hours) - Number(a.hours));
  return items[0] || null;
}

/**
 * The verdict, in one sentence.
 *
 * "The constraint is the approval gate. 619 business hours of removable review
 * wait: 238 h currently owed and 382 h of abnormal tail. #60 (DEL-176) has been
 * in review 144 h, waiting on FrMo." — a claim with its numbers, not a stat grid.
 */
export function constraintSentence(verdict) {
  if (!verdict) return '';
  const constraint = verdict.constraint;
  if (!verdict.available || !constraint) {
    return verdict.note || 'No constraint worth naming this week.';
  }
  const oldest = oldestItem(verdict);
  const tail = Math.round(Number(constraint.tail_hours) || 0);
  const parts = [
    `The constraint is ${stationLabel(constraint)}.`,
    `${poolHours(constraint.removable_hours)} of removable ${constraint.wait_label || 'queue'} wait: ` +
      `${poolHours(constraint.live_hours)} currently owed and ${tail} h of abnormal tail.`,
  ];
  if (oldest) {
    const who = (oldest.holders || []).join(' + ');
    parts.push(
      `${oldest.pr ? `${oldest.pr} (${oldest.key})` : oldest.key} has been in ` +
      `${constraint.wait_label || 'this station'} ${fmtHours(oldest.hours)}` +
      (who ? `, waiting on ${who}` : '') + '.',
    );
  }
  return parts.join(' ');
}

/** The tie sentence, or '' — a tie is never picked silently. */
export function tieNote(verdict) {
  const constraint = verdict && verdict.constraint;
  const tied = (constraint && constraint.tied_with) || [];
  if (!tied.length) return '';
  return `Tied (within 10 %): ${tied.map((row) => `${row.label} ${poolHours(row.removable_hours)}`).join(' · ')} — the card names both, never one.`;
}

/** The meta line: the cohort, the open items, the clock and the snapshot. */
export function metaLine(verdict) {
  if (!verdict) return '';
  const constraint = verdict.constraint || {};
  const liveness = verdict.liveness || {};
  const asOf = verdict.as_of ? fmtAbsolute(Date.parse(verdict.as_of)) : 'the snapshot';
  const n = Number(constraint.n_completed || 0);
  // prd/58: "8 open PRs" is the PR count, never the story count (the two are
  // different objects; the liveness block carries both).
  const open = Number(liveness.open_prs != null ? liveness.open_prs : liveness.open || 0);
  const clock = constraint.clock === 'story' ? 'story clock' : (constraint.clock || 'one clock');
  return `${n} completed stor${n === 1 ? 'y' : 'ies'} · ${open} open PR${open === 1 ? '' : 's'} · ${clock} · as of ${asOf}`;
}

/** The "which clock and why" line that stops the person/role/response ambiguity. */
export function clockLine(verdict) {
  if (!verdict) return '';
  const holder = verdict.holders && verdict.holders.clock === 'pr-holder';
  return 'Station pools are business hours on the story clock (the same segment boundaries ' +
    'the ticket page draws); ' +
    (holder
      ? 'the holder row is the PR ball clock, priced per holder — a different metric that is never added to the pool. '
      : '') +
    'The verdict names the queue, not a person, and never uses response time.';
}

/** The four-row definition list (two rows on a phone; see `.verdict-pools`). */
export function poolRows(verdict) {
  if (!verdict || !verdict.available || !verdict.constraint) return [];
  const constraint = verdict.constraint;
  const holders = verdict.holders || {};
  const sentinel = (holders.people || [])
    .filter((person) => person.role === 'sentinel' && Number(person.hours) > 0)
    .sort((a, b) => Number(b.hours) - Number(a.hours))[0]
    || (holders.people || []).filter((person) => Number(person.hours) > 0)[0];
  const peers = holders.peer_range;
  const holderText = sentinel
    ? `${sentinel.person} ${poolHours(sentinel.hours)}` +
      (peers ? ` · peers ${Math.round(peers[0])}–${Math.round(peers[1])} h` : '')
    : 'no human holder';
  const oldest = oldestItem(verdict);
  const p85 = constraint.p85_hours;
  return [
    { id: 'removable', label: 'removable', value: poolHours(constraint.removable_hours), phone: true },
    { id: 'owed', label: 'owed now', value: owedLabel(constraint), phone: false },
    {
      id: 'tail', label: 'tail',
      value: `${poolHours(constraint.tail_hours)}` +
        (p85 != null ? ` (above p85 ${poolHours(p85)})` : ' (above p85)'),
      phone: false,
    },
    {
      id: 'holders', label: 'holders', value: holderText, phone: false,
      clock: holders.clock === 'pr-holder' ? 'PR clock' : null,
    },
    {
      id: 'oldest', label: 'oldest',
      value: oldest ? `${oldest.pr || ''} ${oldest.key}`.trim() + ` · ${fmtHours(oldest.hours)}` : '—',
      phone: true, phoneOnly: true,
    },
  ];
}

/** The three rule titles a rung shows (the rest stay in the payload/drill-down). */
export function rungRules(rung, limit = 3) {
  return ((rung && rung.rules) || []).slice(0, limit);
}

/** The lab's one-line verdict for a rung (the full prose stays in `note`). */
export function rungLabNote(rung) {
  if (!rung) return '';
  return String(rung.note || '').replace(/\s+/g, ' ').trim();
}

/** `−13 %` with the product's U+2212 minus, or `–` when the lab has no number. */
export function percentText(value) {
  if (value == null || Number.isNaN(Number(value))) return '–';
  const number = Number(value);
  const sign = number > 0 ? '+' : (number < 0 ? '−' : '');
  return `${sign}${Math.abs(number).toFixed(0)} %`;
}

/**
 * The rung's lab line, short enough to read at a glance.
 *
 * The long policy caveats are the rule cards' job; the ladder states the
 * verdict and the number: `review SLA 4 h −9 % cycle · round cap −13 % cycle`,
 * `add a sentinel: no clear effect (−3 % cycle)`. The full note stays on the
 * row's `title` and in the payload.
 */
export function labVerdictLine(lab) {
  if (!lab) return '';
  const label = lab.label || lab.id || 'policy';
  if (lab.verdict === 'no clear effect') {
    return `${label}: no clear effect` + (lab.cycle_percent != null
      ? ` (${percentText(lab.cycle_percent)} cycle)` : '');
  }
  if (lab.verdict === 'not a measured win') {
    return `${label} ${percentText(lab.cycle_percent)} cycle — not a measured win`;
  }
  if (lab.verdict === 'supported' || lab.supports) {
    return `${label} ${percentText(lab.cycle_percent)} cycle`;
  }
  return `${label}: no result in this window`;
}

export function rungLabLine(rung) {
  if (!rung) return '';
  const labs = (rung.labs || []).filter((lab) => lab);
  if (labs.length) return labs.map(labVerdictLine).join(' · ');
  return rungLabNote(rung) || 'the lab has no verdict for this policy in this window';
}

/**
 * The ladder's state pill: exploit open = warning, subordinate open = neutral
 * outline, elevate blocked = neutral outline. **Never red** — a blocked
 * elevation is a decision, not a fire (prd/10 §3.3).
 */
export function rungPill(rung) {
  const state = rung && rung.state;
  const text = state === 'blocked' ? 'blocked' : (state || 'open');
  const kind = state === 'open' && rung && rung.step === 'exploit' ? 'warning' : 'neutral';
  return `<span class="pill ${kind} rung-state" data-rung-state="${esc(state || 'open')}">${esc(text)}</span>`;
}

function rungBody(rung, { base = '', holderText = '' } = {}) {
  const rules = rungRules(rung);
  const labs = (rung.labs || []).filter((lab) => lab.note);
  const note = rungLabNote(rung) || (labs.length ? labs.map((lab) => lab.note).join('; ') : '');
  const items = rung.items || [];
  return `
    <ul class="rung-rules">${rules.length
      ? rules.map((rule) => `<li><button type="button" class="link rung-rule-link"
          data-go="rules" data-anchor="rule-${esc(rule.id)}"
          title="the full rule, its evidence and its drill-down">${esc(rule.title)} →</button></li>`).join('')
      : `<li class="muted">no rule on this rung crosses its threshold in this window</li>`}</ul>
    ${items.length ? `<p class="rung-items-note">the longest current waits · PR ball clock</p>
    <ul class="rung-items">${items.map((item) => `<li>
      <span class="mono">${esc(item.pr || '')}</span>
      ${item.ticket ? ticketLink(item.ticket, { base }) : ''}
      <span class="mono num">${esc(fmtHours(item.hours))}</span>
      ${item.orphaned ? '<span class="pill warning">orphaned approval</span>' : ''}
      ${(item.holders || []).length ? `<span class="muted">${esc(item.holders.join(' + '))}</span>` : ''}
    </li>`).join('')}</ul>` : ''}
    <p class="rung-lab" data-rung-lab>${labs.length
      ? `<span class="lab-tag mono">policy lab</span> ${labs.map((lab) => `<span class="rung-lab-fact" title="${esc(lab.note || '')}">${esc(labVerdictLine(lab))}</span>`).join('<span class="lab-sep" aria-hidden="true"> · </span>')}`
      : `<span class="lab-tag mono">policy lab</span> ${esc(note || 'the lab has no verdict for this policy in this window')}`}</p>
    ${rung.step === 'exploit' && holderText ? `<p class="rung-holder verdict-phone-only">holders
      <span class="mono">${esc(holderText)}</span> <span class="mono clock-tag">PR clock</span></p>` : ''}
    ${rung.step === 'exploit' ? `<p class="rung-honesty">${esc(
      'The model does not see the ticket-level wait, so the exploit rung is the rules\' claim, not the lab\'s.',
    )}</p>` : ''}`;
}

/** The one-line holder split, for the exploit rung on a phone. */
export function holderLine(verdict) {
  const holders = (verdict && verdict.holders) || {};
  const people = holders.people || [];
  const sentinel = people
    .filter((person) => person.role === 'sentinel' && Number(person.hours) > 0)
    .sort((a, b) => Number(b.hours) - Number(a.hours))[0]
    || people.filter((person) => Number(person.hours) > 0)[0];
  if (!sentinel) return '';
  const peers = holders.peer_range;
  return `${sentinel.person} ${poolHours(sentinel.hours)}` +
    (peers ? ` · peers ${poolHours(peers[0])}–${poolHours(peers[1])}` : '');
}

/** The ladder: exploit → subordinate → elevate, each with state and lab note. */
export function ladderMarkup(verdict, { base = '' } = {}) {
  const rungs = (verdict && verdict.ladder) || [];
  if (!rungs.length) return '';
  const holderText = holderLine(verdict);
  return `<ol class="verdict-ladder" data-verdict-ladder>${rungs.map((rung) => {
    const state = rung.state || 'open';
    const blocked = state === 'blocked';
    const items = rung.items || [];
    return `<li class="rung rung-${esc(state)}" data-rung="${esc(rung.step)}" data-rung-state="${esc(state)}">
      ${blocked
        ? `<div class="rung-line">
             <span class="rung-step">${esc(rung.label || rung.step)}</span>
             ${rungPill(rung)}
             <span class="rung-reason">${esc(rung.reason || 'exploit first')}</span>
             <span class="grow"></span>
             <span class="rung-lab-inline muted">lab: ${esc(rungLabLine(rung) || 'no clear effect')}</span>
           </div>
           <p class="rung-blocked-note">No capacity change is proposed while the exploit items are owed:
             run the chase list first, then re-read this rung.</p>`
        : `<details class="rung-details"${rung.step === 'exploit' ? ' open' : ''}>
             <summary class="rung-line">
               <span class="rung-step">${esc(rung.label || rung.step)}</span>
               ${rungPill(rung)}
               <span class="rung-count muted">${rungRules(rung).length} rule${rungRules(rung).length === 1 ? '' : 's'}${
                 items.length ? ` · ${items.length} item${items.length === 1 ? '' : 's'}` : ''}</span>
               <span class="grow"></span>
               ${rung.step === 'exploit' ? '' : `<span class="rung-lab-inline muted">lab: ${esc(rungLabLine(rung))}</span>`}
             </summary>
             <div class="rung-body">${rungBody(rung, { base, holderText })}</div>
           </details>`}
    </li>`;
  }).join('')}</ol>`;
}

/** The footer's "next constraint" ranking behind a `<details>`. */
export function nextConstraint(verdict) {
  const pools = (verdict && verdict.pools) || [];
  const rows = pools.length ? pools : ((verdict && verdict.next) || []);
  const top = verdict && verdict.constraint && verdict.constraint.station;
  return rows.filter((row) => row.station !== top && Number(row.removable_hours) > 0);
}

export function nextLine(verdict) {
  const next = nextConstraint(verdict).slice(0, 3);
  if (!next.length) return 'no other station carries removable wait';
  return next.map((row) => `${(row.label || row.station).toLowerCase()} ${poolHours(row.removable_hours)}`).join(' · ');
}

export function nextMarkup(verdict) {
  const all = nextConstraint(verdict);
  if (!all.length) return '';
  return `<details class="verdict-next" data-next-constraint>
    <summary>Next: ${esc(nextLine(verdict))}</summary>
    <table class="table compact"><thead><tr><th>station</th><th class="num">stories</th>
      <th class="num">removable</th><th class="num">owed now</th>
      <th class="num">tail</th></tr></thead><tbody>${
      all.map((row) => `<tr data-next-station="${esc(row.station)}">
        <td>${esc(row.label || row.station)}</td>
        <td class="num mono">${row.n_completed != null ? esc(String(row.n_completed)) : '–'}</td>
        <td class="num mono">${esc(poolHours(row.removable_hours))}</td>
        <td class="num mono">${esc(poolHours(row.live_hours))}</td>
        <td class="num mono">${esc(poolHours(row.tail_hours))}</td>
      </tr>`).join('')}</tbody></table>
    <p class="panel-note">Every station's removable pool, ranked by the same clock and
      method as the constraint. A lower rank is a smaller fix, not no fix.</p>
  </details>`;
}

/**
 * The Overview card. A full-bleed panel with one claim; when no pool clears one
 * waiting week it says "No constraint worth naming this week" and still shows
 * the ranking (the disclosure is the honest fallback, not a hidden menu).
 */
export function verdictCard(verdict, { base = '' } = {}) {
  if (!verdict) {
    return '<section class="panel wide verdict-panel" id="verdict">'
      + '<div class="empty-row">Loading the constraint verdict…</div></section>';
  }
  const constraint = verdict.constraint;
  const available = Boolean(verdict.available) && Boolean(constraint);
  const pill = available
    ? `<span class="pill warning" data-constraint-pill title="state: wait is owed now">${esc(
      String(constraint.label || 'constraint').toLowerCase())}</span>`
    : '<span class="pill neutral" data-constraint-pill>no verdict</span>';
  const rows = poolRows(verdict);
  return `<section class="panel wide verdict-panel" id="verdict" data-verdict-root data-available="${available}">
    <div class="panel-head"><span class="overline">${OVERLINE}</span> ${pill}
      <span class="grow"></span>
      <button type="button" class="chip" data-verdict-copy
        title="copy the verdict, the ladder and its lab notes as Markdown">Copy as Markdown</button></div>
    <p class="verdict-sentence" data-verdict-sentence>${esc(constraintSentence(verdict))}</p>
    ${tieNote(verdict) ? `<p class="verdict-tie" data-verdict-tie>${esc(tieNote(verdict))}</p>` : ''}
    <p class="verdict-meta">${esc(metaLine(verdict))}</p>
    ${rows.length ? `<dl class="verdict-pools">${rows.map((row) => `
      <div class="vrow${row.phoneOnly ? ' phone-only' : ''}${row.phone ? ' phone-keep' : ' phone-hide'}" data-pool="${esc(row.id)}">
        <dt>${esc(row.label)}${row.clock ? ` <span class="mono clock-tag">${esc(row.clock)}</span>` : ''}</dt>
        <dd class="mono num">${esc(row.value)}</dd>
      </div>`).join('')}</dl>` : ''}
    <p class="verdict-clock" data-verdict-clock>${esc(clockLine(verdict))}</p>
    ${available ? ladderMarkup(verdict, { base }) : ''}
    ${nextMarkup(verdict)}
    <p class="panel-note" data-verdict-basis>${esc(verdict.pool_note || '')}${
      verdict.holders && verdict.holders.available ? ` ${esc(verdict.holder_note || '')}` : ''}</p>
  </section>`;
}

/**
 * The one-line Rules banner: the same verdict, with a link to the ladder.
 * It carries the state, the pool and the clock — never a second number.
 */
export function verdictBanner(verdict) {
  if (!verdict || !verdict.available || !verdict.constraint) return '';
  const constraint = verdict.constraint;
  const n = Number(constraint.open_n || 0);
  const claim = `The constraint is ${stationLabel(constraint)}. ` +
    `${poolHours(constraint.removable_hours)} of removable wait — ` +
    `${poolHours(constraint.live_hours)} owed now on ${n} stor${n === 1 ? 'y' : 'ies'}.`;
  return `<div class="verdict-banner" data-verdict-banner>
    <span class="pill warning">the constraint</span>
    <span class="banner-text"><b>${esc(claim)}</b></span>
    <span class="muted">story clock · ${esc(verdict.as_of ? String(verdict.as_of).slice(0, 10) : 'the snapshot')}</span>
    <button type="button" class="link" data-go="overview" data-anchor="verdict">see the ladder →</button>
  </div>`;
}

/** The Markdown export: the claim, the numbers, the ladder and the lab notes. */
export function verdictMarkdown(verdict, base = '') {
  if (!verdict) return '';
  const constraint = verdict.constraint;
  const lines = ['# The constraint', ''];
  if (!verdict.available || !constraint) {
    lines.push(verdict.note || 'No constraint worth naming this week.', '');
  } else {
    lines.push(constraintSentence(verdict), '');
    lines.push(`_${metaLine(verdict)}_`, '');
    lines.push('| | |', '| --- | --- |');
    for (const row of poolRows(verdict)) {
      if (row.phoneOnly) continue;
      lines.push(`| ${row.label}${row.clock ? ` (${row.clock})` : ''} | ${row.value} |`);
    }
    lines.push('', `_${tieNote(verdict) || 'No tie: the largest removable pool is alone.'}_`, '');
    lines.push(`> ${clockLine(verdict)}`, '');
  }
  for (const rung of verdict.ladder || []) {
    const rules = rungRules(rung, 99);
    lines.push(`## ${rung.label || rung.step} — ${rung.state}`);
    if (rung.reason) lines.push('', `_${rung.reason}_`);
    for (const rule of rules) {
      lines.push(`- ${rule.id}: ${rule.title} — ${fmtImpactHours(rule.impact_hours)} at stake`);
    }
    if (rules.length === 0) lines.push('- no rule on this rung crosses its threshold');
    if (rung.items && rung.items.length) {
      lines.push(`- items: ${rung.items.map((item) => `${item.pr || ''} ${item.ticket || ''} (${fmtHours(item.hours)})`.trim()).join(', ')}`);
    }
    const note = rungLabNote(rung);
    if (note) lines.push(`- policy lab: ${note}`);
    lines.push('');
  }
  const next = nextLine(verdict);
  if (next) lines.push(`_Next: ${next}._`, '');
  lines.push(`_${verdict.pool_note || ''}_`);
  return lines.join('\n');
}

/** The at-stake hours in the export's own compact form (no second formatter). */
export function fmtImpactHours(hours) {
  const value = Number(hours) || 0;
  if (value >= 1000) return `≈${(value / 1000).toFixed(1).replace(/\.0$/, '')}k h`;
  return `≈${fmtHours(value)}`;
}

/* ---------------------------------------------------------- rule rung map
 *
 * Rules' progressive disclosure is driven by the verdict's explicit map, so the
 * browser never re-implements `RUNGS` (one source). An unknown id is `null` —
 * a rule the map cannot place is shown, never hidden.
 */
export function ruleRung(ruleId, verdict) {
  const map = verdict && verdict.rule_rungs;
  if (!map) return null;
  const id = String(ruleId || '');
  if (map.by_rule && map.by_rule[id]) return map.by_rule[id];
  for (const family of map.families || []) {
    if (id.startsWith(family.prefix)) return family.rung;
  }
  return null;
}

/** The rung the page is currently on (the first that is not `done`). */
export function currentRung(verdict) {
  return (verdict && verdict.current_rung) || null;
}

/** Is this suggestion on the current rung (or a `measured` diagnostic)? */
export function isDisclosedByDefault(suggestion, verdict) {
  const rung = ruleRung(suggestion && suggestion.id, verdict);
  if (!rung) return true;              // unplaceable: never hide it
  if (rung === 'measured') return true; // diagnostics keep their ranked position
  return rung === currentRung(verdict);
}

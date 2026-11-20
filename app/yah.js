/* prd/48 — the YAH ("You Are Here") bar.
 *
 * A breadcrumb strip under the top bar: `<tenant> · <view> · <ticket> · <PR>`.
 * It answers *where am I* (location), while the top bar's Back button answers
 * *where was I* (history); the two coexist. Every level but the current one is
 * a jump up the path — not just one step back — so the ticket's story and its
 * view are one click away from the deepest drawer.
 *
 * The path is pure data: `serializeRoute` writes the URL each level lands on,
 * so `node --test` can assert the segments without a browser. `collapseYah`
 * is the phone rule (tenant + view + current), applied by the renderer.
 */

import { serializeRoute } from './route.js';

/** The levels, shallowest first. */
export const YAH_LEVELS = ['tenant', 'view', 'ticket', 'pr'];

function step(level, label, title, href) {
  return { level, label, title, href };
}

/**
 * Build the location path.
 *
 * `view`/`viewLabel` are the level *above* the ticket: on a ticket page the
 * caller passes the view the ticket was opened from, so the path reads
 * `del-combined · Delivery list · DEL-167` and that segment is a real jump
 * back to the list. `pr` is `{ number, repo }` while a PR drawer is open; the
 * drawer is not in the URL, so its segment lands on the ticket (or the view).
 */
export function yahPath({
  tenant = null, view = null, viewLabel = null,
  ticket = null, pr = null,
  timeframe = null, scope = null, sprint = null,
} = {}) {
  const steps = [];
  const sprintParam = view === 'timeline' ? sprint : null;

  steps.push(step(
    'tenant',
    tenant || 'no dataset',
    tenant ? `dataset ${tenant} — its front page` : 'no dataset loaded',
    // A tenant level is the dataset's front page: a scope is per tenant
    // (prd/12), so it must not ride along into another identity space.
    `/${serializeRoute({ tenant, view: tenant ? 'overview' : null, timeframe })}`,
  ));

  if (view) {
    const label = viewLabel || view;
    steps.push(step(
      'view',
      label,
      `the ${label} view`,
      `/${serializeRoute({ tenant, view, timeframe, scope, sprint: sprintParam })}`,
    ));
  }

  if (ticket) {
    steps.push(step(
      'ticket',
      ticket,
      `ticket ${ticket}`,
      `/${serializeRoute({ tenant, view: 'ticket', ticket, timeframe, scope })}`,
    ));
  }

  if (pr && pr.number != null) {
    const repo = pr.repo || null;
    // The drawer has no route of its own: its level is the page underneath.
    steps.push(step(
      'pr',
      `#${pr.number}`,
      repo ? `PR #${pr.number} in ${repo}` : `PR #${pr.number}`,
      ticket
        ? `/${serializeRoute({ tenant, view: 'ticket', ticket, timeframe, scope })}`
        : `/${serializeRoute({ tenant, view, timeframe, scope, sprint: sprintParam })}`,
    ));
  }

  return steps;
}

/**
 * The phone rule (prd/28 §1, prd/48): a 412px strip cannot hold four
 * segments, so keep the nearest ancestors — the dataset and the view — and
 * the current level. Three or fewer segments already fit.
 */
export function collapseYah(steps = []) {
  if (steps.length <= 3) return steps.slice();
  const kept = [steps[0], steps[1], steps[steps.length - 1]];
  return kept.filter((item, index) =>
    kept.findIndex((candidate) => candidate.level === item.level) === index);
}

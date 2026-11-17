# 28 — Mobile use (assessment)

## Status: desktop-first, by design — mobile is not supported

`prd/10`'s user model is explicit: "Large monitors, often several… full-width layouts." The UI has a
viewport meta and one medium-screen breakpoint (`@media (max-width: 1180px)`), but no phone breakpoint,
no touch gesture handling, and the core surfaces (canvas timeline, blocking graph, dense tables, the
detail drawer) assume a mouse + a wide viewport. This is a desktop process-mining/analytics tool, not a
mobile app.

## What breaks on a phone (to verify)

- Canvas timeline/graph: wheel/box-zoom + drag pan are mouse-first; pinch/tap are unsupported.
- Dense tables (delivery, drill-down, rules): horizontal overflow, tiny tap targets.
- The detail drawer and sidebar: fixed-width, not a bottom-sheet / off-canvas.
- The top bar (scope/timeframe/tenant/density) doesn't wrap.

## Decision

**Keep v1 desktop-only.** Mobile support is a substantial separate effort (responsive shell, touch
gestures, canvas simplification, list-first mobile views) with little payoff for an internal
delivery-flow analytics tool. If it's ever wanted, the scope is:

1. Responsive shell: collapsible/off-canvas sidebar, wrapped top bar, bottom-sheet detail panel.
2. Touch: pinch-to-zoom + one-finger pan on the timeline, tap-to-drill on the graph.
3. Simplified mobile views: list-first (delivery/rules/ticket list) with the canvas as a secondary
   "open in wide view" affordance, rather than cramming the full canvas onto a phone.

## Out of scope

Mobile-optimising the canvas itself (it stays a desktop artifact); native apps.

# 28 — Responsive design (desktop + phone)

The UI must be **optimal on both** a desktop monitor and a OnePlus IN2025-class phone
(~412×915 CSS viewport). One codebase, two layouts, via breakpoints + touch detection.

## Principles

- **Desktop keeps the current dense, canvas-first experience** (prd/10) — unchanged.
- **Phone is list-first**: the entry is the ticket / delivery / rules list; the timeline and blocking
  graph are secondary "explore" views that still work with touch (pinch/pan), not crammed.
- The phone's primary use case is the **ticket detail** — "why did DEL-167 take 3 weeks" — so that page
  must be fully usable one-handed.

## Breakpoints

- `>1180px` desktop (current). · `641–1180px` medium (collapsible sidebar, current). · `≤640px` **phone** (new).

## Phone layout (≤640px)

1. **Shell** — sidebar → off-canvas (hamburger + slide-in + scrim); top bar → wraps to two rows
   (scope/timeframe/tenant/density as pill groups); detail panel → full-width **bottom sheet** (swipe
   to dismiss).
2. **Touch** — pinch-to-zoom + one-finger pan on the timeline and graph; tap to select/drill; no
   hover-only affordances (tooltips become tap-and-hold); tap targets ≥44px.
3. **List-first** — the default phone entry is the **Delivery list** (or a compact "needs attention"
   list) + the **ticket detail**; Timeline/Blocking/Queue/Rules reachable via the drawer.
4. **Tables** — on phone, stack rows into key-value cards (or horizontal scroll with a pinned first
   column); never horizontal-overflow the viewport.

## Definition of done

- Every view renders and is usable at 412×915 (no horizontal overflow, all controls reachable,
  tap targets ≥44px).
- Touch gestures work on the timeline (pinch/pan) and blocking graph (pan/tap).
- The ticket detail page is fully usable on phone.
- Desktop is unchanged (regression screenshot at 1600px + the existing node tests stay green).

## Out of scope

Native apps · tablet-specific layout (640–1024 = the medium breakpoint).

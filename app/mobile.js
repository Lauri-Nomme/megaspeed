/* Phone shell (prd/28): one codebase, two layouts.
 *
 * Desktop keeps the dense canvas-first experience (prd/10) untouched; at
 * `<= 640px` this module provides the phone shell — an off-canvas sidebar, a
 * full-width bottom sheet for detail panels, and touch gestures. The pure
 * helpers (`phoneEntryPage`, `hintText`, `shouldDismissSheet`, `sheetOffset`)
 * are exported so `node --test` can assert the decisions without a browser.
 *
 * Nothing here invents data: it only moves the same panels around.
 */

export const PHONE_MAX = 640;
/** A bottom sheet closes past this drag distance, or on a quick flick. */
export const SHEET_DISMISS_PX = 88;
export const SHEET_FLICK_PX = 28;
export const SHEET_FLICK_VELOCITY = 0.45; // px / ms

export function isPhoneWidth(width) {
  const value = Number(width);
  return Number.isFinite(value) && value <= PHONE_MAX;
}

/** True when the current viewport is the phone breakpoint. */
export function isPhone() {
  if (typeof window === 'undefined') return false;
  return isPhoneWidth(window.innerWidth);
}

/* prd/28 §3: phone is list-first. The Delivery list is the entry unless the
 * user has already chosen a page (a restored preference wins — the phone is not
 * allowed to hide the page someone deliberately opened). */
export function phoneEntryPage(savedPage = null) {
  return savedPage || 'delivery';
}

/* The phone timeline opens on the story hierarchy: its gutter labels are the
 * Jira keys (epic → story), and zooming in nests each PR with its own key and
 * number. The actor dimension hides every ticket key below eight lanes, which
 * is the phone complaint this default fixes. A saved choice still wins. */
export function phoneEntryLanes(savedLanes = null) {
  return savedLanes || 'story';
}

/* Gestures cannot use the desktop hint line: a phone has no wheel, no arrow
 * keys and no right-click. Say what the finger can actually do. */
export function hintText(which, phone = false) {
  if (which === 'timeline') {
    return phone
      ? 'drag to pan · pinch to zoom · tap to select · hold for details'
      : 'drag to pan · wheel to zoom · arrows pan · +/− zoom · Home fits';
  }
  if (which === 'delivery') {
    return phone
      ? 'tap a row to inspect · hold to pivot'
      : 'click a row to inspect · right-click to pivot';
  }
  if (which === 'blocking') {
    return phone ? 'drag to pan · pinch to zoom · tap a node to open its queue · hold for details' : '';
  }
  return '';
}

/** Swipe-to-dismiss decision for the bottom sheets (prd/28 §1). */
export function shouldDismissSheet({ dy = 0, velocity = 0, distance = SHEET_DISMISS_PX } = {}) {
  if (!(dy > 0)) return false;
  if (dy >= distance) return true;
  return dy >= SHEET_FLICK_PX && velocity >= SHEET_FLICK_VELOCITY;
}

/** Which way a one-finger drag is going, once it passes the slop.
 *
 * A canvas is a gesture surface, but a phone must still be scrollable: the
 * first few pixels decide whether the finger owns a horizontal pan/zoom or the
 * page owns a vertical scroll. `null` means "not a gesture yet"; the caller
 * keeps the axis it picked for the rest of the drag. */
export function touchAxis(dx = 0, dy = 0, slop = 8) {
  if (Math.hypot(Number(dx) || 0, Number(dy) || 0) <= slop) return null;
  return Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
}

/** The live drag offset of a sheet: down only, never up past its resting place. */
export function sheetOffset(dy = 0) {
  return dy > 0 ? `translateY(${Math.round(dy)}px)` : '';
}

/** Which sheet a touch target belongs to, or null. */
export function sheetForTarget(target) {
  if (!target || typeof target.closest !== 'function') return null;
  return target.closest('.detail, .drawer');
}

/* ------------------------------------------------------------------ DOM */

function addClass(el, name) {
  if (el) el.classList.add(name);
}

function removeClass(el, name) {
  if (el) el.classList.remove(name);
}

/**
 * Wire the phone shell. Called once from main.js after the DOM exists.
 *
 * `onSheetDismiss(el)` runs when a bottom sheet is swiped away or its close
 * button is used; the page owns the state that opened it.
 */
export function initMobile({ onSheetDismiss } = {}) {
  const body = typeof document !== 'undefined' ? document.body : null;
  if (!body) return { openNav() {}, closeNav() {}, toggleNav() {}, isNavOpen: () => false };

  const toggle = document.getElementById('nav-toggle');
  const scrim = document.getElementById('scrim');
  const sidebar = document.getElementById('sidebar');

  const isNavOpen = () => body.classList.contains('nav-open');

  function setNav(open) {
    const next = Boolean(open);
    body.classList.toggle('nav-open', next);
    if (toggle) toggle.setAttribute('aria-expanded', String(next));
    if (scrim) scrim.hidden = !next;
    if (sidebar) sidebar.setAttribute('aria-hidden', String(!next && isPhone()));
    if (next && toggle) {
      const first = sidebar && sidebar.querySelector('.nav-item');
      if (first) first.focus();
    }
  }

  if (toggle) {
    toggle.addEventListener('click', () => setNav(!isNavOpen()));
  }
  if (scrim) scrim.addEventListener('click', () => setNav(false));
  // A navigation is a dismissal: the drawer must not cover the page it opened.
  document.addEventListener('click', (event) => {
    if (event.target.closest && event.target.closest('#sidebar .nav-item')) setNav(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && isNavOpen()) setNav(false);
  });
  // Leaving the phone breakpoint must not leave the page in a half-open state.
  if (typeof window !== 'undefined' && window.matchMedia) {
    const media = window.matchMedia(`(max-width: ${PHONE_MAX}px)`);
    const onChange = () => { if (!media.matches) setNav(false); };
    if (media.addEventListener) media.addEventListener('change', onChange);
    else if (media.addListener) media.addListener(onChange);
  }
  if (sidebar && isPhone()) sidebar.setAttribute('aria-hidden', 'true');

  // Bottom sheets: the PR drawer and the delivery detail panel.
  const sheets = [document.getElementById('drawer'), document.getElementById('delivery-detail')];
  for (const sheet of sheets) {
    if (sheet) attachSheet(sheet, () => {
      if (onSheetDismiss) onSheetDismiss(sheet);
    });
  }

  return {
    openNav: () => setNav(true),
    closeNav: () => setNav(false),
    toggleNav: () => setNav(!isNavOpen()),
    isNavOpen,
  };
}

/** Swipe-down-to-dismiss on a bottom sheet, touch only. */
export function attachSheet(sheet, onDismiss) {
  if (!sheet || typeof sheet.addEventListener !== 'function') return;
  let drag = null;

  sheet.addEventListener('pointerdown', (event) => {
    if (event.pointerType === 'mouse') return;
    if (!isPhone()) return;
    // A scrolled list scrolls; it does not drag the sheet away.
    if (sheet.scrollTop > 0) return;
    drag = {
      id: event.pointerId,
      y: event.clientY,
      x: event.clientX,
      last: event.clientY,
      t: performance.now(),
      dy: 0,
      active: false,
    };
  });

  sheet.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const dy = event.clientY - drag.y;
    const dx = event.clientX - drag.x;
    if (!drag.active) {
      // only a downward, mostly-vertical drag moves the sheet
      if (dy < 8 || Math.abs(dx) > Math.abs(dy)) return;
      drag.active = true;
      sheet.classList.add('dragging');
      sheet.style.transition = 'none';
    }
    drag.dy = Math.max(0, dy);
    drag.last = event.clientY;
    sheet.style.transform = sheetOffset(drag.dy);
    event.preventDefault();
  });

  function end(event) {
    if (!drag || event.pointerId !== drag.id) return;
    const { dy, last, t } = drag;
    const elapsed = Math.max(1, performance.now() - t);
    const velocity = (last - drag.y) / elapsed;
    const wasActive = drag.active;
    drag = null;
    sheet.classList.remove('dragging');
    sheet.style.transition = '';
    sheet.style.transform = '';
    if (wasActive && shouldDismissSheet({ dy, velocity })) {
      if (onDismiss) onDismiss();
    }
  }

  sheet.addEventListener('pointerup', end);
  sheet.addEventListener('pointercancel', end);

  // The close button is a 44px target on the phone; it dismisses the same way.
  sheet.addEventListener('click', (event) => {
    const close = event.target.closest && event.target.closest('[data-sheet-close]');
    if (!close) return;
    event.preventDefault();
    event.stopPropagation();
    if (onDismiss) onDismiss();
  });
}

/**
 * Long-press (tap-and-hold) on a row or canvas: the touch equivalent of the
 * desktop right-click pivot menu (prd/28 §2: no hover-only affordances).
 */
export function bindLongPress(handler, { ms = 520, slop = 12 } = {}) {
  if (typeof document === 'undefined') return;
  let timer = 0;
  let start = null;
  let fired = false;
  let firedTarget = null;

  const cancel = () => {
    clearTimeout(timer);
    timer = 0;
    start = null;
  };

  document.addEventListener('pointerdown', (event) => {
    if (event.pointerType === 'mouse') return;
    const target = event.target;
    const row = target.closest && target.closest('tr.row[data-id], [data-drill-item]');
    const canvas = target.closest && target.closest('canvas');
    if (!row && !canvas) return;
    fired = false;
    firedTarget = null;
    start = { x: event.clientX, y: event.clientY, target, id: event.pointerId };
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!start) return;
      fired = true;
      firedTarget = start.target;
      if (handler) handler(start.target, start.x, start.y);
    }, ms);
  }, { passive: true });

  document.addEventListener('pointermove', (event) => {
    if (!start || event.pointerId !== start.id) return;
    if (Math.hypot(event.clientX - start.x, event.clientY - start.y) > slop) cancel();
  }, { passive: true });

  document.addEventListener('pointerup', (event) => {
    if (start && event.pointerId === start.id) cancel();
  }, { passive: true });
  document.addEventListener('pointercancel', cancel, { passive: true });

  // A long press must not also count as a click on the row underneath. The
  // menu it opened is a different target, so its own taps still work.
  document.addEventListener('click', (event) => {
    if (!fired) return;
    const target = firedTarget;
    fired = false;
    firedTarget = null;
    if (target && target.contains && target.contains(event.target)) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
}

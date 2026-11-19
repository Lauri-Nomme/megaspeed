/* Theme tokens: dark is the default, light is a first-class alternative
 * (prd/10 §3.1). The craft is in the dark one, so nothing here changes it.
 *
 * Every colour the app paints — CSS or canvas — comes from one CSS custom
 * property. `style.css` owns the values: `:root` is the dark palette and
 * `[data-theme="light"]` overrides it. This module is the bridge for the two
 * places CSS cannot reach:
 *
 *   1. Canvas 2D code needs resolved colour strings, not `var()` references, so
 *      `refreshTokens()` reads them with `getComputedStyle` and `color(name)`
 *      hands them out. Modules register a palette applier with `onPalette()`:
 *      it runs once for the dark fallbacks (so `node --test` and a first paint
 *      before the stylesheet resolves both work) and again on every theme
 *      switch, which is what recolours the canvases without a data reload.
 *   2. The preference has to be applied before the first paint, so it is
 *      stored under its own key (`megaspeed.theme`) that the inline boot script
 *      in `index.html` can read without waiting for this module.
 *
 * `TOKEN_FALLBACK` mirrors the `:root` block. `app/test/theme.test.mjs` asserts
 * that parity, so the two cannot drift: dark stays byte-identical.
 */

export const THEMES = ['dark', 'light'];
export const DEFAULT_THEME = 'dark';
/** Its own key, not `megaspeed.ui.v1`: the pre-paint boot script reads it. */
export const THEME_KEY = 'megaspeed.theme';

/** `'light' | 'dark' | null` — unknown values are ignored, never trusted. */
export function normalizeTheme(value) {
  if (value == null) return null;
  const text = String(value).trim().toLowerCase();
  return THEMES.includes(text) ? text : null;
}

/** The theme a toggle press switches to. */
export function otherTheme(theme) {
  return normalizeTheme(theme) === 'light' ? 'dark' : 'light';
}

/** The glyph the toggle shows for the *current* theme. */
export function themeGlyph(theme) {
  return normalizeTheme(theme) === 'light' ? '☀' : '☾';
}

/** What pressing the toggle does, in words (title + aria-label). */
export function themeLabel(theme) {
  return normalizeTheme(theme) === 'light'
    ? 'switch to the dark theme'
    : 'switch to the light theme';
}

/* ------------------------------------------------------------------ storage */

function safeStorage() {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null; // a blocked localStorage throws on access, not on use
  }
}

/** The stored preference, or null (dark) when there is none or it is unreadable. */
export function readTheme(storage = safeStorage()) {
  try {
    return normalizeTheme(storage && storage.getItem(THEME_KEY));
  } catch {
    return null;
  }
}

/** Persist the choice; private mode and quota failures are not worth a banner. */
export function storeTheme(theme, storage = safeStorage()) {
  const next = normalizeTheme(theme);
  if (!next) return null;
  try {
    if (storage) storage.setItem(THEME_KEY, next);
  } catch { /* private mode */ }
  return next;
}

/* -------------------------------------------------------------------- tokens */

/* Dark values — verbatim copies of the `:root` block in style.css. A canvas
 * module imported by a node test has no `document`; these are what it draws
 * with until the browser resolves the real values. */
export const TOKEN_FALLBACK = {
  // grounds and chrome
  '--bg-app': '#191B1C',
  '--bg-sidebar': '#1E2023',
  '--bg-surface': '#232528',
  '--bg-surface-raised': '#292B2E',
  '--bg-hover': '#303235',
  '--bg-control': '#2F3132',
  '--bg-control-active': '#3B3F42',
  '--border-hairline': 'rgba(255,255,255,.06)',
  '--border-control': '#3A3D41',
  '--text-primary': '#E8E9EB',
  '--text-secondary': '#919397',
  '--text-tertiary': '#8A8D93',
  '--popover-bg': '#242733',

  // canvas ground + shared furniture
  '--canvas-bg': '#151924',
  '--canvas-overlay': 'rgba(22,24,37,.92)',
  '--canvas-veil': 'rgba(21,25,36,.6)',
  '--canvas-edge': 'rgba(70,160,200,.30)',
  '--canvas-region-label': '#8C98D4',
  '--canvas-hairline': 'rgba(255,255,255,.06)',
  '--canvas-axis-line': 'rgba(255,255,255,.08)',
  '--canvas-grid-major': 'rgba(255,255,255,.10)',
  '--canvas-grid-minor': 'rgba(255,255,255,.045)',
  '--canvas-grid-faint': 'rgba(255,255,255,.05)',
  '--canvas-weekend': 'rgba(255,255,255,.022)',
  '--canvas-night': 'rgba(0,0,0,.18)',
  '--canvas-row': 'rgba(255,255,255,.012)',
  '--canvas-row-rule': 'rgba(255,255,255,.035)',
  '--canvas-row-hover': 'rgba(99,177,255,.07)',
  '--canvas-row-hover-soft': 'rgba(255,255,255,.025)',
  '--canvas-row-selected': 'rgba(99,177,255,.08)',
  '--canvas-strong': '#E8E9EB',
  '--canvas-label': '#B9BBC0',
  '--canvas-muted': '#8A8D93',
  '--canvas-faint': '#6B6F76',
  '--canvas-faintest': '#4E5258',
  '--canvas-bucket': 'rgba(232,233,235,.16)',
  '--canvas-bucket-strong': 'rgba(232,233,235,.5)',
  '--canvas-mean': 'rgba(232,233,235,.22)',
  '--canvas-band': 'rgba(140,152,212,.055)',
  '--canvas-band-alt': 'rgba(140,152,212,.022)',
  '--canvas-band-focus': 'rgba(104,192,252,.14)',
  '--canvas-band-dim': 'rgba(140,152,212,.015)',
  '--canvas-spine': 'rgba(140,152,212,.30)',
  '--canvas-epic': 'rgba(140,152,212,.16)',
  '--canvas-epic-edge': 'rgba(140,152,212,.65)',
  '--canvas-baseline': 'rgba(138,141,147,.28)',
  '--canvas-hist': 'rgba(99,177,255,.55)',
  '--canvas-hist-weak': 'rgba(99,177,255,.30)',
  '--canvas-brush-veil': 'rgba(104,192,252,.14)',
  '--canvas-brush': 'rgba(104,192,252,.35)',
  '--canvas-brush-idle': 'rgba(104,192,252,.22)',
  '--canvas-brush-edge': 'rgba(104,192,252,.7)',
  '--canvas-node-fill': '#191C29',
  '--canvas-edge-dim': 'rgba(120,130,150,.12)',
  '--canvas-edge-legend': 'rgba(120,130,150,.9)',
  '--canvas-dim-ring': 'rgba(255,255,255,.12)',
  '--canvas-dim-arc': 'rgba(255,255,255,.06)',
  '--canvas-plan-line': 'rgba(251,205,88,.45)',
  '--canvas-carry': 'rgba(251,205,88,.9)',

  // semantic state
  '--critical': '#F05C70',
  '--critical-tint': '#47282F',
  '--critical-border': '#A02C4C',
  '--critical-veil': 'rgba(240,92,112,.14)',
  '--warning': '#FBCD58',
  '--warning-tint': '#3D3524',
  '--warning-veil': 'rgba(251,205,88,.14)',
  '--healthy': '#73CF55',
  '--healthy-veil': 'rgba(115,207,85,.12)',
  '--info-accent': '#63B1FF',
  '--info-bg': '#252B31',
  '--link': '#94B4D4',
  '--link-strong': '#7FB0EA',
  '--primary-tint-bg': '#1B3046',
  '--primary-tint-fg': '#68C0FC',
  '--focus-ring': '#68C0FC',

  // direction pair (reserved: teal = waits on, amber = waited on by)
  '--dir-waits-on': '#3CB4AA',
  '--dir-waited-by': '#D9A24E',

  // categories + series
  '--cat-1': '#73CF55',
  '--cat-2': '#53C9DF',
  '--cat-3': '#B565E8',
  '--cat-4': '#E25AB5',
  '--cat-5': '#E8B84A',
  '--cat-6': '#C04B56',
  '--cat-7': '#8A8D93',
  '--series-default': '#238FF9',
  '--series-warning': '#FBCD58',
  '--series-error': '#F15D71',

  // the reserved lens gradient, as seven stops a canvas can interpolate
  '--lens-1': '#1F3A66',
  '--lens-2': '#2878A4',
  '--lens-3': '#3CB49C',
  '--lens-4': '#9CD45A',
  '--lens-5': '#F2C14E',
  '--lens-6': '#F08A3C',
  '--lens-7': '#E8455A',

  // phase vocabulary (the timeline gantt + the ticket segment bar)
  '--phase-waiting': '#E8B84A',
  '--phase-active': '#B565E8',
  '--phase-review': '#63B1FF',
  '--phase-ci': '#53C9DF',
  '--phase-rebasing': '#E25AB5',
  '--phase-closed': '#8A8D93',
  '--phase-unobserved': '#70747B',
  '--phase-in-review': '#4C8DFF',
  '--phase-in-verification': '#2FBFA0',
};

const tokens = { ...TOKEN_FALLBACK };

/** Resolve a token name to its current colour (`--canvas-bg` → `#151924`). */
export function color(name) {
  return tokens[name] || TOKEN_FALLBACK[name] || TOKEN_FALLBACK['--cat-7'];
}

/**
 * Read every known token out of a `getComputedStyle`-like object. Pure: the
 * tests hand it a fake, the browser hands it the document's style.
 */
export function readTokens(style) {
  const out = {};
  for (const name of Object.keys(TOKEN_FALLBACK)) {
    const raw = style && typeof style.getPropertyValue === 'function'
      ? style.getPropertyValue(name)
      : '';
    out[name] = String(raw == null ? '' : raw).trim() || TOKEN_FALLBACK[name];
  }
  return out;
}

/**
 * Re-read the palette from the document (after `data-theme` changed) and let
 * every registered canvas module recolour itself. Returns the token map.
 */
export function refreshTokens(root = typeof document === 'undefined' ? null : document.documentElement) {
  if (!root || typeof getComputedStyle !== 'function') return { ...tokens };
  Object.assign(tokens, readTokens(getComputedStyle(root)));
  return tokens;
}

const appliers = [];

/**
 * Register a palette applier for a canvas module. It runs immediately (dark
 * fallbacks) and on every `refreshPalette()` — so a module's colours are never
 * frozen at import time.
 */
export function onPalette(fn) {
  appliers.push(fn);
  fn();
  return fn;
}

/** Re-read the tokens, then re-apply every registered palette. */
export function refreshPalette(root) {
  refreshTokens(root);
  for (const fn of appliers) fn();
  return tokens;
}

/** Set `data-theme` on a root element; returns the theme actually applied. */
export function applyTheme(theme, root = typeof document === 'undefined' ? null : document.documentElement) {
  const next = normalizeTheme(theme) || DEFAULT_THEME;
  if (root && root.dataset) root.dataset.theme = next;
  return next;
}

/** `#RRGGBB` / `rgb(...)` → `[r, g, b]`, or null when it is neither. */
export function rgbOf(value) {
  const text = String(value == null ? '' : value).trim();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(text);
  if (hex) {
    const digits = hex[1].length === 3
      ? hex[1].split('').map((c) => c + c).join('')
      : hex[1];
    const n = parseInt(digits, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const rgb = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(text);
  if (rgb) return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
  return null;
}

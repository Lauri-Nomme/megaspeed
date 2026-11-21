/* The light theme (prd/10 §3.1): tokens, the toggle's pure helpers, and the
 * guards that keep colour out of the renderers.
 *
 * Three things are asserted here:
 *   1. the preference: dark is the default, light is stored under its own key,
 *      a `?theme=` link wins, and an unreadable storage is not a crash;
 *   2. the palette: `TOKEN_FALLBACK` is the `:root` block byte for byte (so
 *      dark cannot drift), every colour token has a light value, and the light
 *      values keep the 4.5:1 contrast prd/10 §11 asks for;
 *   3. the canvas: switching theme re-resolves the hues from the tokens (no
 *      data reload), and no canvas module contains a colour literal — the
 *      source guard that keeps this true after the next feature.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  DEFAULT_THEME, THEMES, THEME_KEY, TOKEN_FALLBACK, applyTheme, color, normalizeTheme,
  otherTheme, readTheme, readTokens, refreshPalette, rgbOf, storeTheme,
  themeGlyph, themeLabel,
} from '../theme.js';
import { ACTIVITY_COLOR, PHASE, applyPalette } from '../timeline.js';
import { SEGMENT_PHASE } from '../ticket.js';
import { ROLE_COLOR } from '../graph.js';
import { DIAGNOSIS } from '../queues.js';
import { parseRoute, serializeRoute } from '../route.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/* ---- the stylesheet, as data ------------------------------------------- */

/** The declarations of the first block that starts with `selector {`. */
function block(selector) {
  const css = read('style.css');
  const start = css.indexOf(`${selector} {`);
  assert.ok(start >= 0, `${selector} is not in style.css`);
  const end = css.indexOf('\n}', start);
  const body = css.slice(start, end);
  const out = {};
  for (const match of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    out[match[1]] = match[2].replace(/\s+/g, '').toLowerCase();
  }
  return out;
}

const ROOT = block(':root');
const LIGHT = block('[data-theme="light"]');

const isColour = (value) => value.startsWith('#') || value.startsWith('rgb');

/* ---- 1. the preference ------------------------------------------------- */

test('dark is the default and light is a first-class alternative', () => {
  assert.equal(DEFAULT_THEME, 'dark');
  assert.deepEqual(THEMES, ['dark', 'light']);
  assert.equal(THEME_KEY, 'megaspeed.theme');
  assert.equal(normalizeTheme('light'), 'light');
  assert.equal(normalizeTheme(' LIGHT '), 'light');
  assert.equal(normalizeTheme('dark'), 'dark');
  // nothing else is a theme: a corrupted preference falls back to dark
  for (const value of [null, undefined, '', 'system', 'sepia', 42]) {
    assert.equal(normalizeTheme(value), null);
  }
  assert.equal(otherTheme('dark'), 'light');
  assert.equal(otherTheme(null), 'light');
  assert.equal(otherTheme('light'), 'dark');
  assert.equal(themeGlyph('dark'), '☾');
  assert.equal(themeGlyph('light'), '☀');
  assert.match(themeLabel('dark'), /light theme/);
  assert.match(themeLabel('light'), /dark theme/);
});

test('the choice survives a reload, and a broken storage is not a crash', () => {
  const store = new Map();
  const storage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
  };
  assert.equal(readTheme(storage), null); // no preference yet: dark
  assert.equal(storeTheme('light', storage), 'light');
  assert.equal(readTheme(storage), 'light');
  assert.equal(storeTheme('dark', storage), 'dark');
  assert.equal(readTheme(storage), 'dark');
  // an unknown value is ignored, never written
  assert.equal(storeTheme('sepia', storage), null);
  assert.equal(readTheme(storage), 'dark');

  const broken = {
    getItem() { throw new Error('blocked'); },
    setItem() { throw new Error('quota'); },
  };
  assert.equal(readTheme(broken), null);
  assert.equal(storeTheme('light', broken), 'light'); // the session still switches
  assert.equal(readTheme(null), null);
});

test('applyTheme writes data-theme and refuses an unknown value', () => {
  const root = { dataset: {} };
  assert.equal(applyTheme('light', root), 'light');
  assert.equal(root.dataset.theme, 'light');
  assert.equal(applyTheme('dark', root), 'dark');
  assert.equal(root.dataset.theme, 'dark');
  assert.equal(applyTheme('sepia', root), 'dark');
  assert.equal(root.dataset.theme, 'dark');
});

/* ---- 2. the palette ---------------------------------------------------- */

test('the dark fallbacks are the :root block, byte for byte', () => {
  // `theme.js` cannot read CSS in a node test or a pre-stylesheet paint; it
  // carries a copy. A copy that drifts is a dark-theme regression.
  const names = Object.keys(TOKEN_FALLBACK);
  assert.ok(names.length > 80, `only ${names.length} tokens`);
  for (const name of names) {
    assert.ok(name in ROOT, `${name} is missing from :root`);
    assert.equal(TOKEN_FALLBACK[name].replace(/\s+/g, '').toLowerCase(), ROOT[name],
      `${name} differs between theme.js and :root`);
  }
  // no token may define itself through var(): the canvas reads the resolved
  // value, and a self-reference resolves to nothing
  for (const [name, value] of Object.entries(ROOT)) {
    assert.doesNotMatch(value, /var\(/, `${name} is an alias, not a value`);
  }
});

test('every colour token has a light value, and light is not an alias', () => {
  const colours = Object.entries(ROOT).filter(([, value]) => isColour(value));
  assert.ok(colours.length > 90, `only ${colours.length} colour tokens`);
  for (const [name, dark] of colours) {
    assert.ok(name in LIGHT, `${name} is not re-pointed by [data-theme="light"]`);
    assert.notEqual(LIGHT[name], dark, `${name} is the dark value in the light theme`);
    assert.ok(isColour(LIGHT[name]), `${name} is not a colour in the light theme`);
  }
});

test('light text and state hues keep 4.5:1 on every ground', () => {
  const luminance = (hex) => {
    const [r, g, b] = rgbOf(hex);
    const channel = (value) => {
      const c = value / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  };
  const contrast = (a, b) => {
    const la = luminance(a);
    const lb = luminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  };
  const grounds = ['--bg-app', '--bg-surface', '--canvas-bg', '--bg-sidebar'];
  // anything used as text: chrome, state, links, categories, phases, canvas labels
  const ink = [
    '--text-primary', '--text-secondary', '--text-tertiary', '--link',
    '--primary-tint-fg', '--critical', '--warning', '--healthy', '--info-accent',
    '--cat-1', '--cat-2', '--cat-3', '--cat-4', '--cat-5', '--cat-6', '--cat-7',
    '--dir-waits-on', '--dir-waited-by', '--phase-waiting', '--phase-active',
    '--phase-review', '--phase-ci', '--phase-rebasing', '--phase-closed',
    '--phase-in-review', '--phase-in-verification',
    '--canvas-strong', '--canvas-label', '--canvas-muted', '--canvas-faint',
    '--canvas-region-label',
  ];
  for (const name of ink) {
    assert.ok(isColour(LIGHT[name]), `${name} has no light value`);
    for (const ground of grounds) {
      const ratio = contrast(LIGHT[name], LIGHT[ground]);
      assert.ok(ratio >= 4.5, `${name} on ${ground} is ${ratio.toFixed(2)}:1 in light`);
    }
  }
});

test('readTokens takes the document values and falls back to dark', () => {
  const style = {
    getPropertyValue(name) {
      if (name === '--canvas-bg') return '  #EFF3F8 ';
      return '';
    },
  };
  const tokens = readTokens(style);
  assert.equal(tokens['--canvas-bg'], '#EFF3F8');
  // a stylesheet that has not loaded yet must not paint with empty strings
  assert.equal(tokens['--canvas-faint'], TOKEN_FALLBACK['--canvas-faint']);
  assert.equal(readTokens(null)['--text-primary'], TOKEN_FALLBACK['--text-primary']);
});

/* ---- 3. the canvas ----------------------------------------------------- */

const styleOf = (map) => ({ getPropertyValue: (name) => map[name] || '' });

/** Resolve the app's palettes through the real refresh path. */
function usePalette(map) {
  const previous = globalThis.getComputedStyle;
  globalThis.getComputedStyle = () => styleOf(map);
  try {
    refreshPalette({});
  } finally {
    globalThis.getComputedStyle = previous;
  }
}

test('a theme switch recolours the canvases from the tokens, no data reload', () => {
  // the parser lowercases CSS values; the palette keeps them as resolved
  const same = (actual, expected) => assert.equal(
    String(actual).toLowerCase(), String(expected).toLowerCase(),
  );
  usePalette(LIGHT);
  try {
    same(ACTIVITY_COLOR.pr_opened, LIGHT['--cat-2']);
    same(ACTIVITY_COLOR.pr_merged, LIGHT['--series-default']);
    same(PHASE.review.color, LIGHT['--phase-review']);
    same(PHASE.in_verification.color, LIGHT['--phase-in-verification']);
    same(SEGMENT_PHASE.in_progress_pr_open.color, LIGHT['--phase-active']);
    same(SEGMENT_PHASE.merge_verification.color, PHASE.in_review.color);
    same(ROLE_COLOR.review, LIGHT['--info-accent']);
    same(DIAGNOSIS.overloaded.color, LIGHT['--critical']);
    same(DIAGNOSIS.overloaded.tint, LIGHT['--critical-veil']);
    same(color('--canvas-bg'), LIGHT['--canvas-bg']);
  } finally {
    usePalette(ROOT); // leave the process on the default palette
  }
  // ... and switching back restores the dark hues exactly
  same(ACTIVITY_COLOR.pr_opened, TOKEN_FALLBACK['--cat-2']);
  same(PHASE.review.color, TOKEN_FALLBACK['--phase-review']);
  same(DIAGNOSIS.slow.tint, TOKEN_FALLBACK['--warning-veil']);
});

test('every canvas hue is a token, and every token resolves to a colour', () => {
  applyPalette();
  for (const [activity, value] of Object.entries(ACTIVITY_COLOR)) {
    assert.ok(isColour(value), `activity ${activity} is not a colour`);
  }
  for (const [phase, entry] of Object.entries(PHASE)) {
    assert.ok(isColour(entry.color), `phase ${phase} is not a colour`);
    assert.ok(entry.token.startsWith('--'), `phase ${phase} has no token`);
  }
  for (const [key, entry] of Object.entries(SEGMENT_PHASE)) {
    assert.ok(isColour(entry.color), `segment ${key} is not a colour`);
  }
});

test('no canvas module contains a colour literal', () => {
  // The hues live in style.css and nowhere else: a hex in a renderer is a
  // colour the light theme cannot recover. `theme.js` is the only exception
  // (its fallback table is asserted against :root above).
  for (const file of ['timeline.js', 'graph.js', 'queues.js', 'ticket.js']) {
    const src = read(file);
    assert.doesNotMatch(src, /['"]#[0-9a-f]{3,8}['"]/i, `${file} has a hex colour`);
    assert.doesNotMatch(src, /['"]rgba?\(\s*\d/i, `${file} has an rgb() colour`);
    assert.match(src, /from '\.\/theme\.js'/, `${file} does not import the theme`);
    assert.match(src, /color\('--[a-z0-9-]+'\)/, `${file} does not read a token`);
  }
  // the phase + activity vocabularies are declared as token maps
  const timeline = read('timeline.js');
  assert.match(timeline, /ACTIVITY_TOKEN = \{/);
  assert.match(timeline, /token: '--phase-/);
  assert.match(timeline, /onPalette\(applyPalette\)/);
  assert.match(read('ticket.js'), /token: '--phase-/);
  assert.match(read('graph.js'), /onPalette\(applyPalette\)/);
  assert.match(read('queues.js'), /onPalette\(applyPalette\)/);
});

test('rgbOf reads the token values the browser hands out', () => {
  assert.deepEqual(rgbOf('#1F3A66'), [31, 58, 102]);
  assert.deepEqual(rgbOf('#FFF'), [255, 255, 255]);
  assert.deepEqual(rgbOf('rgb(31,58,102)'), [31, 58, 102]);
  assert.deepEqual(rgbOf('rgba(31, 58, 102, .5)'), [31, 58, 102]);
  assert.equal(rgbOf('transparent'), null);
  assert.equal(rgbOf(''), null);
  assert.equal(rgbOf(null), null);
});

/* ---- 4. the route + the wiring ----------------------------------------- */

test('P-2/prd/10: a light view is shareable, dark is the default URL', () => {
  const light = serializeRoute({ tenant: 'del', view: 'timeline', theme: 'light' });
  assert.equal(light, '?tenant=del&view=timeline&theme=light');
  assert.equal(parseRoute(light, '').theme, 'light');
  // dark costs nothing: every URL written before themes existed still works
  assert.equal(serializeRoute({ tenant: 'del', view: 'timeline', theme: 'dark' }), '?tenant=del&view=timeline');
  assert.equal(serializeRoute({ tenant: 'del', theme: null }), '?tenant=del');
  assert.equal(parseRoute('?theme=dark', '').theme, 'dark');
  assert.equal(parseRoute('?theme=sepia', '').theme, null);
  // the ticket hash and the theme coexist
  const ticket = serializeRoute({ view: 'ticket', ticket: 'DEL-1', theme: 'light' });
  assert.equal(ticket, '?view=ticket&theme=light#ticket/DEL-1');
  const parsed = parseRoute('?view=ticket&theme=light', '#ticket/DEL-1');
  assert.equal(parsed.view, 'ticket');
  assert.equal(parsed.theme, 'light');
});

test('the page wires the toggle, the boot order and the URL', () => {
  const html = read('index.html');
  const main = read('main.js');
  const css = read('style.css');

  // the control: one small button in the top bar, next to density
  assert.match(html, /id="theme-toggle"/);
  assert.match(html, /class="theme-toggle"/);
  assert.match(html, /aria-pressed="false"/);
  assert.ok(html.indexOf('id="theme-toggle"') < html.indexOf('id="density"'),
    'the toggle is not next to density');

  // no flash: the inline boot script runs before the stylesheet paints
  const script = html.indexOf("localStorage.getItem('megaspeed.theme')");
  assert.ok(script > 0, 'the pre-paint theme script is missing');
  assert.ok(script < html.indexOf('<link rel="stylesheet"'), 'the theme resolves after the stylesheet');
  assert.match(html, /document\.documentElement\.setAttribute\('data-theme', theme\)/);

  // the wiring: one path for the button, the URL and the boot
  assert.match(main, /function setTheme\(theme, \{ save = true, repaint = true \} = \{\}\)/);
  assert.match(main, /state\.theme = applyTheme\(next, document\.documentElement\)/);
  assert.match(main, /refreshPalette\(document\.documentElement\)/);
  assert.match(main, /if \(save\) storeTheme\(state\.theme\)/);
  assert.match(main, /timeline\.schedule\(\);\n    blockingMap\.schedule\(\);\n    queueChart\.schedule\(\);/);
  assert.match(main, /\$\('theme-toggle'\)\.addEventListener\('click'/);
  assert.match(main, /setTheme\(otherTheme\(state\.theme\)\)/);
  // applied before the first paint, from URL → data-theme → storage → dark.
  // prd/63 F29: a `?theme=` link is a view, not a preference — it is never
  // written to storage (only the toggle saves).
  assert.match(main, /setTheme\(\n  initialRoute\.theme\n    \|\| normalizeTheme\(document\.documentElement\.dataset\.theme\)\n    \|\| readTheme\(\)\n    \|\| DEFAULT_THEME,\n  \{ save: false, repaint: false \},/);
  assert.match(main, /if \(route\.theme && route\.theme !== state\.theme\) setTheme\(route\.theme, \{ save: false \}\)/);
  // the capture carries the theme onto its clone (the SVG root is the wrapper)
  assert.match(main, /wrapper\.setAttribute\('data-theme', document\.documentElement\.dataset\.theme \|\| 'dark'\)/);
  // the route carries it, and a `?theme=` link is applied on navigation
  assert.match(main, /theme: state\.theme === 'light' \? 'light' : null/);
  assert.match(main, /if \(route\.theme && route\.theme !== state\.theme\) setTheme\(route\.theme, \{ save: false \}\)/);
  assert.match(main, /setTheme,\n  get theme\(\) \{ return state\.theme; \}/);

  // the CSS: a light block that re-points the same names
  assert.match(css, /\[data-theme="light"\] \{/);
  assert.match(css, /\.theme-toggle \{/);
  assert.match(css, /\.theme-toggle\[aria-pressed="true"\]/);
});

test('the toggle is reachable and sized for a 412px phone', () => {
  const css = read('style.css');
  const phone = css.slice(css.indexOf('@media (max-width: 640px)'));
  // the first row: hamburger, brand, theme, density, status; context below
  assert.match(phone, /\.theme-toggle \{ order: 3; width: 44px; min-width: 44px;/);
  assert.match(phone, /\.density \{ order: 4; \}/);
  assert.match(phone, /\.topbar \.status \{ order: 5;/);
  assert.match(phone, /\.ctx-row \{[^}]*order: 6;/s);
  // a light theme must repaint the phone's grounds too, not just the desktop's
  assert.match(css, /\[data-theme="light"\] \{[^}]*--bg-app: #F3F5F8;/s);
});

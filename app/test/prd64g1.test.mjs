/* prd/64 G1 — phone layout & touch targets (F-01, F-02).
 *
 * The QA pass found two phone-only failures that no unit test could see because
 * they live in CSS only:
 *
 *   F-01  `.delivery` (min-width: 1120px) and `.response-panel .response-table`
 *         (900px) kept their desktop minimum inside the ≤640px stack, so every
 *         stacked card was 900–1120px wide and its value sat at x ≈ 880–1100.
 *   F-02  six interactive controls were 16–36px tall on a phone (prd/28 asks
 *         for ≥44px): the Blocking text-view summary, the policy-lab knob, the
 *         YAH step, the ticket-jump input, the skip link and the timeline
 *         carry-over summary.
 *
 * These are source guards: they read `app/style.css` the way a browser would —
 * comments stripped, at-rule nesting tracked — so a rule that is silently
 * overridden by a desktop one, or a new wide `min-width` that forgets the phone
 * reset, fails here instead of on a 412px screenshot.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const SOURCE = readFileSync(new URL('../style.css', import.meta.url), 'utf8');

/* ---- a tiny stylesheet reader ----------------------------------------- */

const stripComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, '');

function declarations(body) {
  const out = [];
  for (const chunk of body.split(';')) {
    const at = chunk.indexOf(':');
    if (at < 0) continue;
    out.push([
      chunk.slice(0, at).trim(),
      chunk.slice(at + 1).trim().replace(/\s+/g, ' '),
    ]);
  }
  return out;
}

/** Every style rule with the at-rule context it sits in. */
function parseStylesheet(source) {
  const text = stripComments(source);
  const rules = [];
  const media = [];
  let buffer = '';
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === '{') {
      const prelude = buffer.trim();
      buffer = '';
      if (prelude.startsWith('@')) {
        media.push(prelude);
        i += 1;
        continue;
      }
      const end = text.indexOf('}', i);
      rules.push({
        selectors: prelude.split(',').map((s) => s.trim().replace(/\s+/g, ' ')).filter(Boolean),
        declarations: declarations(text.slice(i + 1, end)),
        media: [...media],
      });
      i = end + 1;
      continue;
    }
    if (ch === '}') {
      media.pop();
      buffer = '';
      i += 1;
      continue;
    }
    buffer += ch;
    i += 1;
  }
  return rules;
}

const RULES = parseStylesheet(SOURCE);
const phone = (rule) => rule.media.some((query) => /^@media\s*\(max-width:\s*640px\)$/.test(query)
  || /\(max-width:\s*640px\)/.test(query));
const value = (rule, prop) => {
  const hit = rule.declarations.filter(([name]) => name === prop);
  return hit.length ? hit[hit.length - 1][1] : null;
};
const phoneRules = RULES.filter(phone);
const matches = (rule, selector) => rule.selectors.some((s) => selector.test(s));

/** The phone rule that applies `prop: expected` to `selector`, if any. */
function phoneDeclaration(selector, prop, expected) {
  return phoneRules.find((rule) => matches(rule, selector)
    && value(rule, prop) === expected);
}

/** Every `min-width` declaration whose largest px number exceeds the phone. */
function wideDeclarations() {
  const out = [];
  for (const rule of RULES) {
    for (const [prop, raw] of rule.declarations) {
      if (prop !== 'min-width') continue;
      const px = [...raw.matchAll(/(-?\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
      if (!px.length || Math.max(...px) <= 412) continue;
      for (const selector of rule.selectors) out.push({ selector, raw, rule });
    }
  }
  return out;
}

/* The view containers F-01 is about: a table (or the wrapper/panel that owns
 * one) that must fit 412px once its rows stack. A popover is not one. */
const VIEW_CONTAINER = /\b(delivery|table|table-wrap|drill|response-table|response-panel|queue)\b/;

/* ---- F-01: phone cards fit the viewport ------------------------------- */

test('prd/64 F-01: the delivery list has no desktop minimum on a phone', () => {
  assert.ok(
    phoneDeclaration(/^\.delivery$/, 'min-width', '0'),
    'the ≤640px block must carry `.delivery { min-width: 0 }`',
  );
  // the desktop table still needs its 1120px to lay eight columns out
  assert.ok(
    RULES.some((rule) => !phone(rule) && matches(rule, /^\.delivery$/)
      && value(rule, 'min-width') === '1120px'),
    'the desktop `.delivery { min-width: 1120px }` must stay',
  );
});

test('prd/64 F-01: the queue response table has no desktop minimum on a phone', () => {
  assert.ok(
    phoneDeclaration(/^\.response-panel \.response-table$/, 'min-width', '0'),
    'the ≤640px block must carry `.response-panel .response-table { min-width: 0 }`',
  );
  assert.ok(
    RULES.some((rule) => !phone(rule)
      && matches(rule, /^\.response-panel \.response-table$/)
      && value(rule, 'min-width') === '900px'),
    'the desktop `.response-panel .response-table { min-width: 900px }` must stay',
  );
  // the panel repeats its column widths with a specificity the card reset does
  // not beat; a pinned 84–190px cell squeezes the label/value pair on a card
  for (const cell of ['.c-dur', '.c-who']) {
    const selector = new RegExp(`^\\.response-panel \\.response-table \\${cell}$`);
    assert.ok(
      phoneRules.some((rule) => matches(rule, selector) && value(rule, 'width') === 'auto'),
      `the ≤640px block must reset ${cell} to \`width: auto\``,
    );
  }
});

test('prd/64 F-01: no wide min-width on a table container escapes the phone block', () => {
  const wide = wideDeclarations();
  // prove the scan is not vacuous: it finds the three desktop minimums
  for (const [selector, raw] of [
    ['.delivery', '1120px'],
    ['.response-panel .response-table', '900px'],
    ['.drill', 'min(760px, 100%)'],
  ]) {
    assert.ok(
      wide.some((entry) => entry.selector === selector && entry.raw === raw),
      `the audit should see \`${selector} { min-width: ${raw} }\``,
    );
  }
  const unreset = wide
    .filter((entry) => VIEW_CONTAINER.test(entry.selector))
    .filter((entry) => !phoneRules.some((rule) => rule.selectors.includes(entry.selector)
      && value(rule, 'min-width') === '0'))
    .map((entry) => `${entry.selector} { min-width: ${entry.raw} }`);
  assert.deepEqual(
    unreset, [],
    `a table/view container wider than 412px needs \`min-width: 0\` in the ≤640px block: ${unreset.join(', ')}`,
  );
});

/* ---- F-02: phone touch targets are 44px ------------------------------- */

const TOUCH_TARGETS = [
  ['Blocking text-view summary', /^\.blocking-text > summary$/],
  ['policy-lab knob row', /^\.policy-knob$/],
  // the checkbox is 13px wide; the label beside it pads the row instead
  ['policy-lab knob label', /^\.policy-knob-k$/],
  ['YAH step', /^\.yah-step$/],
  ['ticket-jump input', /^\.ticket-jump input$/],
  // same row as the input, and it was 36px too
  ['ticket-jump button', /^\.ticket-jump button$/],
  ['skip link', /^\.skip-link$/],
  ['timeline carry-over summary', /^\.sprint-carry-summary > summary$/],
];

test('prd/64 F-02: every listed phone control carries a 44px target', () => {
  for (const [label, selector] of TOUCH_TARGETS) {
    assert.ok(
      phoneDeclaration(selector, 'min-height', '44px'),
      `the ≤640px block must give ${label} \`min-height: 44px\``,
    );
  }
});

test('prd/64 F-02: desktop density is untouched', () => {
  const desktop = (selector, prop) => RULES.filter((rule) => !phone(rule) && matches(rule, selector))
    .map((rule) => value(rule, prop));
  // the dense 20px YAH step and 34px control height stay on a desktop
  assert.ok(desktop(/^\.yah-step$/, 'height').includes('20px'));
  assert.ok(desktop(/^\.ticket-jump input$/, 'height').includes('var(--control-h)'));
  // the label was never a full-row target on a desktop
  assert.ok(!desktop(/^\.policy-knob-k$/, 'min-height').includes('44px'));
});

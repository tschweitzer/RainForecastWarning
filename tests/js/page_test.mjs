/* Behavioural tests for the JavaScript the signup page ships.
 *
 * Takes the served `/static/signup.js` as argv[2], pulls the functions out of it and runs them.
 * The script is not a module and touches `document` at load, so it cannot simply be imported;
 * extracting by brace-matching is the cheap way to test the parts that are pure logic.
 *
 * argv[2] was the rendered page until the script moved out of `index.html` into a static file.
 * Reading the served file rather than the one on disk keeps the property that made the rendered
 * page the right input: it is what a browser is handed, so a route that stopped serving it, or
 * served something else, fails here rather than in production.
 *
 * These exist because the alternative was asserting that substrings appear in the HTML, and that
 * kind of test passed while `sameKey` would have destroyed a working subscription on every signup
 * on a browser that reports an empty applicationServerKey. */
import assert from 'node:assert/strict';
import fs from 'node:fs';

const body = fs.readFileSync(process.argv[2], 'utf8');
/* The settings page, when given: `usesOurKey` there mirrors `sameKey` here, and two copies of a
   comparison that decides whether a subscription is usable is exactly the pair that drifts. It is
   still the rendered page, because manage.html still carries its script inline. */
const managed = process.argv[3] ? fs.readFileSync(process.argv[3], 'utf8') : null;

function extract(name, source = body) {
  const at = source.indexOf('function ' + name + '(');
  assert.notEqual(at, -1, `the page no longer defines ${name}() - has it been renamed?`);
  let depth = 0, started = false;
  for (let i = at; i < source.length; i++) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') { depth--; if (started && depth === 0) return source.slice(at, i + 1); }
  }
  throw new Error(`unbalanced braces in ${name}()`);
}

const sandbox = {};
new Function('sandbox', `${extract('keyBytes')}\n${extract('sameKey')}\n` +
  'sandbox.keyBytes = keyBytes; sandbox.sameKey = sameKey;')(sandbox);
const { keyBytes, sameKey } = sandbox;

const KEY = 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkTCkLnCJ0i5H8Y_tJc5Hbv3bYlV0dBQdLHzWcAkLSVRxDDQ2cCLFxE';
const bytes = keyBytes(KEY);
const other = keyBytes(KEY.slice(0, -1) + (KEY.slice(-1) === 'A' ? 'B' : 'A'));

const results = [];
function test(name, fn) {
  try { fn(); results.push(['ok', name]); }
  catch (e) { results.push(['FAIL', name, e.message]); }
}

test('a base64url key decodes to the 65 bytes P-256 requires', () => {
  assert.equal(bytes.length, 65);
  assert.equal(bytes[0], 0x04, 'an uncompressed P-256 point starts with 0x04');
});

test('the same key is reused', () => {
  assert.equal(sameKey({ options: { applicationServerKey: bytes.buffer } }, KEY), true);
});

test('a different key of the same length is replaced', () => {
  assert.equal(sameKey({ options: { applicationServerKey: other.buffer } }, KEY), false);
});

test('a truncated key is replaced', () => {
  assert.equal(sameKey({ options: { applicationServerKey: bytes.slice(0, 32).buffer } }, KEY), false);
});

// The four "we cannot tell" shapes. All must reuse: discarding a subscription we cannot prove is
// stale costs the reader their settings, which is worse than the stale-key case it guards against.
test('a subscription with no options is reused', () => {
  assert.equal(sameKey({}, KEY), true);
});

test('options without a key is reused', () => {
  assert.equal(sameKey({ options: {} }, KEY), true);
});

test('a null key is reused', () => {
  assert.equal(sameKey({ options: { applicationServerKey: null } }, KEY), true);
});

test('an empty ArrayBuffer is reused, not treated as a mismatch', () => {
  // Truthy, so `!key` did not catch it; length 0 !== 65 made it a "mismatch" and unsubscribed a
  // working subscription on every signup attempt.
  assert.equal(sameKey({ options: { applicationServerKey: new ArrayBuffer(0) } }, KEY), true);
});

test('a key that is not an ArrayBuffer at all is reused', () => {
  assert.equal(sameKey({ options: { applicationServerKey: KEY } }, KEY), true);
});

// --- the settings page's copy of the same decision ---
if (managed) {
  const box = {};
  new Function('box', `const VAPID_KEY = ${JSON.stringify(KEY)};\n` +
    `${extract('usesOurKey', managed)}\nbox.usesOurKey = usesOurKey;`)(box);
  const { usesOurKey } = box;

  const shapes = [
    ['our key', { options: { applicationServerKey: bytes.buffer } }, true],
    ['a different key', { options: { applicationServerKey: other.buffer } }, false],
    ['a truncated key', { options: { applicationServerKey: bytes.slice(0, 32).buffer } }, false],
    ['no options', {}, true],
    ['options without a key', { options: {} }, true],
    ['a null key', { options: { applicationServerKey: null } }, true],
    ['an empty ArrayBuffer', { options: { applicationServerKey: new ArrayBuffer(0) } }, true],
    ['a key that is not an ArrayBuffer', { options: { applicationServerKey: KEY } }, true]
  ];
  for (const [label, sub, want] of shapes) {
    test(`/manage agrees with / on ${label}`, () => {
      assert.equal(usesOurKey(sub), want);
      // The two must not merely both be defined - they must decide the same way, or a subscription
      // is usable on one page and dead on the other.
      assert.equal(usesOurKey(sub), sameKey(sub, KEY));
    });
  }
}

/* --- the remembered radar window ------------------------------------------------------------
 *
 * `storedWindow` reads a value a *previous version of this page* wrote, which makes it the one
 * input here that is neither the server's nor the reader's typing - so an old or hand-edited
 * value must not become a window nobody offers. It also has to survive a browser that throws on
 * localStorage rather than returning null, which is what Safari does in private browsing and what
 * any browser with site data blocked does.
 *
 * These run the functions with a fake `window` and `CONFIG`. That deliberately cannot catch the
 * ordering bug that shipped here - `WINDOW_KEY` declared below its first use, read as `undefined`,
 * every stored preference silently ignored - because extraction throws the ordering away. That one
 * is guarded in the source instead: `const` makes it stop the page rather than pass a wrong value
 * on, and tests/test_pages.py asserts these stay `const`. */
function windowFns(store, config) {
  const box = {};
  const fakeWindow = { localStorage: store };
  new Function('box', 'window', 'CONFIG', 'WINDOW_KEY',
    `${extract('storedWindow')}\n${extract('initialWindow')}\n${extract('rememberWindow')}\n` +
    'box.storedWindow = storedWindow; box.initialWindow = initialWindow;' +
    'box.rememberWindow = rememberWindow;'
  )(box, fakeWindow, config, 'rainalert.windowHours');
  return box;
}

const DEFAULTS = { windowHours: 12, maxHours: 48, windowPinned: false };
const fakeStore = (value) => {
  const held = { value };
  return {
    getItem: () => (held.value === undefined ? null : held.value),
    setItem: (k, v) => { held.value = v; },
    held
  };
};

test('a stored window is read back', () => {
  assert.equal(windowFns(fakeStore('24'), DEFAULTS).storedWindow(), 24);
});

test('nothing stored means no preference, not zero', () => {
  assert.equal(windowFns(fakeStore(undefined), DEFAULTS).storedWindow(), null);
});

for (const [label, stored] of [['rubbish', 'twelve'], ['empty', ''], ['zero', '0'],
                               ['negative', '-3'], ['past the ceiling', '999']]) {
  test(`a ${label} stored window is ignored`, () => {
    assert.equal(windowFns(fakeStore(stored), DEFAULTS).storedWindow(), null);
  });
}

test('a browser that throws on localStorage is not a broken page', () => {
  // The whole reason these are wrapped: this runs during page setup, so an uncaught throw here
  // would take the map and the signup form with it, to remember a slider position.
  const hostile = { getItem() { throw new DOMException('denied'); },
                    setItem() { throw new DOMException('denied'); } };
  const fns = windowFns(hostile, DEFAULTS);
  assert.equal(fns.storedWindow(), null);
  assert.doesNotThrow(() => fns.rememberWindow(24));
});

test('with no preference the page opens on the server default', () => {
  assert.equal(windowFns(fakeStore(undefined), DEFAULTS).initialWindow(), 12);
});

test('a stored preference beats the server default', () => {
  assert.equal(windowFns(fakeStore('24'), DEFAULTS).initialWindow(), 24);
});

test('a shared ?hours= link beats the stored preference', () => {
  // Someone sending "look at the last 48 hours" is not asking about your settings.
  const pinned = { windowHours: 48, maxHours: 48, windowPinned: true };
  assert.equal(windowFns(fakeStore('24'), pinned).initialWindow(), 48);
});

test('the preference is written back as a plain number', () => {
  const store = fakeStore(undefined);
  windowFns(store, DEFAULTS).rememberWindow(6);
  assert.equal(store.held.value, '6');
});

let failed = 0;
for (const [status, name, message] of results) {
  if (status === 'FAIL') { failed++; console.log(`FAIL ${name}: ${message}`); }
  else { console.log(`ok   ${name}`); }
}
console.log(failed ? `\n${failed} of ${results.length} failed` : `\nall ${results.length} passed`);
process.exit(failed ? 1 : 0);

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

// --- the one reader of an existing subscription (D-67) ---
/* The settings page used to carry its own copy of this decision (`usesOurKey`), tested here for
   agreement with `sameKey`. Since D-67 the settings are on this page and state B, the settings and
   the signup all read the subscription through `ownSubscription`, so what is tested is that one
   reader: it hands out only a subscription made with our key, and fails closed to null. */
async function ownWith({ registration, key = KEY, throws = false }) {
  const box = {};
  const navigatorFake = {
    serviceWorker: {
      getRegistration: async () => {
        if (throws) { throw new Error('boom'); }
        return registration;
      }
    }
  };
  new Function('box', 'navigator', 'window',
    `const VAPID_KEY = ${JSON.stringify(key)};\n${extract('keyBytes')}\n${extract('sameKey')}\n` +
    `${extract('pushSupported')}\nasync ${extract('ownSubscription')}\nbox.own = ownSubscription;`
  )(box, navigatorFake, { PushManager: {}, Notification: {} });
  return box.own();
}
const subWith = (buffer) => ({ endpoint: 'https://push.example/x', options: { applicationServerKey: buffer } });
const registrationWith = (sub) => ({ pushManager: { getSubscription: async () => sub } });
const asyncResults = [];
async function testAsync(name, fn) {
  try { await fn(); asyncResults.push(['ok', name]); }
  catch (e) { asyncResults.push(['FAIL', name, e.message]); }
}
await testAsync('a subscription made with our key is handed out', async () => {
  const sub = subWith(bytes.buffer);
  assert.equal(await ownWith({ registration: registrationWith(sub) }), sub);
});
await testAsync('a subscription made with another key is not', async () => {
  assert.equal(await ownWith({ registration: registrationWith(subWith(other.buffer)) }), null);
});
await testAsync('no worker registered means no subscription', async () => {
  assert.equal(await ownWith({ registration: undefined }), null);
});
await testAsync('a browser that throws means no subscription, not a broken page', async () => {
  assert.equal(await ownWith({ registration: undefined, throws: true }), null);
});
await testAsync('without a VAPID key there is nothing to send to', async () => {
  assert.equal(await ownWith({ registration: registrationWith(subWith(bytes.buffer)), key: '' }), null);
});
results.push(...asyncResults.splice(0));

/* --- what the page shows below the map (D-67) -------------------------------------------------
 *
 * `decideOnce` is all async flow - a timeout racing a slow settings answer, a second link arriving
 * mid-run, a picker left from the signup - and a test that checks the source's shape cannot see any
 * of it. So it runs here, against a fake settings module, a fake page and timers scaled down a
 * hundredfold (the 8 s guard becomes 80 ms). */
function stateMachine({ subscription = null, begin, hash = '' }) {
  const store = {};
  const doc = { getElementById: (id) => (store[id] = store[id] || { id, hidden: true, textContent: '' }) };
  const elements = new Proxy({}, { get: (target, id) => doc.getElementById(id) });
  const held = { subscription };
  const log = { replaced: [], begins: [], cleared: 0, maxActive: 0, active: 0 };
  const win = {
    location: { hash, pathname: '/', search: '' },
    setTimeout: (fn, ms) => setTimeout(fn, ms / 100),
    console: { error: () => {} },
    RainSettings: null
  };
  const hist = { replaceState: (a, b, url) => { log.replaced.push(url); win.location.hash = ''; } };
  const fakePick = { clear: () => { log.cleared++; } };
  const box = {};
  new Function('box', 'document', 'window', 'history', 'ownSubscriptionFake', 'fakePick',
    `var pageState = null, deciding = false, decideAgain = false, map = null, pick = null;
     var EMAIL_AVAILABLE = false;
     function ownSubscription() { return Promise.resolve(ownSubscriptionFake.subscription); }
     function addLocate() {}
     function ensurePicker() { pick = pick || fakePick; return pick; }
     ${extract('reveal')}
     ${extract('accountNote')}
     ${extract('within')}
     async ${extract('decideSignupState')}
     async ${extract('decideOnce')}
     var carryNote = null;
     var RainPage = { showSettings: function () { reveal('settings'); } };
     box.decide = decideSignupState;
     box.state = function () { return pageState; };
     box.page = RainPage;`
  )(box, doc, win, hist, held, fakePick);
  win.RainSettings = {
    begin: async (page, options) => {
      log.begins.push(options);
      log.active++; log.maxActive = Math.max(log.maxActive, log.active);
      try { return await begin(page, options); } finally { log.active--; }
    }
  };
  return { ...box, elements, log, held };
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await testAsync('nobody subscribed: the signup, at once', async () => {
  const m = stateMachine({ begin: async () => ({ opened: false, note: null }) });
  await m.decide();
  assert.equal(m.state(), 'signup');
  assert.equal(m.elements['signup-section'].hidden, false);
  assert.equal(m.elements['einstellungen'].hidden, true);
  assert.equal(m.elements['already-subscribed'].hidden, true);
});

await testAsync('a session that turns up late replaces the signup with the settings', async () => {
  const m = stateMachine({ begin: async (page) => { await sleep(20); page.showSettings(); return { opened: true, note: null }; } });
  const run = m.decide();
  await sleep(5);
  assert.equal(m.state(), 'signup', 'fail-open: the signup is shown while asking');
  await run;
  assert.equal(m.state(), 'settings');
  assert.equal(m.elements['signup-section'].hidden, true);
  assert.equal(m.elements['einstellungen'].hidden, false);
});

await testAsync('a subscribed browser waits on "Einen Moment", not on a signup form', async () => {
  const m = stateMachine({ subscription: {}, begin: async () => { await sleep(20); return { opened: false, note: null }; } });
  const run = m.decide();
  await sleep(5);
  assert.equal(m.state(), null);
  assert.equal(m.elements['account-busy'].hidden, false);
  assert.equal(m.elements['signup-section'].hidden, true);
  await run;
  assert.equal(m.state(), 'subscribed');
  assert.equal(m.elements['account-busy'].hidden, true);
});

await testAsync('a slow answer: state B meanwhile, and its late note still arrives', async () => {
  const m = stateMachine({ subscription: {}, begin: async () => { await sleep(150); return { opened: false, note: 'spät' }; } });
  const run = m.decide();
  await sleep(110);   // past the (scaled) 8 s guard
  assert.equal(m.state(), 'subscribed');
  await run;
  assert.equal(m.elements['account-note'].textContent, 'spät');
  assert.equal(m.elements['account-note'].hidden, false);
  assert.equal(m.elements['account-busy'].hidden, true);
});

await testAsync('a second decision while one is slow waits for it - never two redemptions at once', async () => {
  const m = stateMachine({ subscription: {}, begin: async () => { await sleep(150); return { opened: false, note: null }; } });
  const first = m.decide();
  await sleep(110);   // the guard has fired; the first begin is still running
  await m.decide();
  await first;
  assert.equal(m.log.maxActive, 1, 'two begin() calls overlapped');
  assert.equal(m.log.begins.length, 2, 'the second request must still run, after the first');
});

await testAsync('a settings link is read and erased before anything is awaited', async () => {
  let seen = null;
  const m = stateMachine({ hash: '#t=abc%2Ddef', begin: async (page, options) => { seen = options.token; return { opened: false, note: null }; } });
  const run = m.decide();
  assert.deepEqual(m.log.replaced, ['/'], 'the token must leave the address bar synchronously');
  await run;
  assert.equal(seen, 'abc-def');
});

await testAsync('state B takes a pin left over from the signup off the map', async () => {
  const m = stateMachine({ begin: async () => ({ opened: false, note: null }) });
  await m.decide();
  assert.equal(m.state(), 'signup');
  assert.equal(m.log.cleared, 0, 'the signup keeps its pin');
  // Signed up since, and the settings cannot open here (a browser that could not keep a key).
  m.held.subscription = {};
  await m.decide();
  assert.equal(m.state(), 'subscribed');
  assert.equal(m.log.cleared, 1, 'a draggable pin in state B would move nothing');
});
results.push(...asyncResults.splice(0));

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

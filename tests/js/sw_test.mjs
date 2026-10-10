/* Behavioural tests for the service worker. Run by tests/test_service_worker.py, which is how they
   reach CI; runnable on their own with `node tests/js/sw_test.mjs` from the repo root. */
import assert from 'node:assert/strict';
import { loadWorker, fire } from './sw_harness.mjs';

const results = [];
async function test(name, fn) {
  try { await fn(); results.push(['ok', name]); }
  catch (e) { results.push(['FAIL', name, e.message]); }
}

const WARNING = {
  title: 'Regen in etwa 12 Minuten',
  body: 'Leichter Regen zieht auf.',
  url: '/#l=tok'
};

/* What a notification shown before D-64 still carries in the tray: a button whose request token
   points at a route that no longer exists. */
const OLD_WARNING = {
  ...WARNING,
  actions: [{ title: 'Einstellungen', url: '/api/v1/manage/request', body: '{}' }]
};

await test('a push shows a notification carrying the payload', async () => {
  const w = loadWorker({ maxActions: 2 });
  await fire(w.listeners.push, { data: { json: () => WARNING } });
  assert.equal(w.shown.length, 1);
  assert.equal(w.shown[0].title, 'Regen in etwa 12 Minuten');
  assert.equal(w.shown[0].options.body, 'Leichter Regen zieht auf.');
});

await test('no buttons are drawn, even for a payload that still names some', async () => {
  // Notifications carry no buttons (D-64): settings and unsubscribing live on the settings page.
  const w = loadWorker({ maxActions: 2 });
  await fire(w.listeners.push, { data: { json: () => OLD_WARNING } });
  assert.equal(w.shown[0].options.actions, undefined);
});

await test('an unparseable payload still shows something', async () => {
  const w = loadWorker({ maxActions: 2 });
  await fire(w.listeners.push, { data: { json: () => { throw new Error('bad json'); } } });
  // Chrome shows "This site has been updated in the background" if we show nothing.
  assert.equal(w.shown.length, 1);
  assert.equal(w.shown[0].title, 'Regenwarnung');
});

await test('a push with no data at all still shows something', async () => {
  const w = loadWorker({ maxActions: 2 });
  await fire(w.listeners.push, { data: null });
  assert.equal(w.shown.length, 1);
});

await test('the icon and badge are the PNGs, not an SVG', async () => {
  const w = loadWorker({ maxActions: 2 });
  await fire(w.listeners.push, { data: { json: () => WARNING } });
  // Chrome renders no SVG in a notification, and the badge is a monochrome mask.
  assert.equal(w.shown[0].options.icon, '/static/icon-192.png');
  assert.equal(w.shown[0].options.badge, '/static/badge-96.png');
});

await test('tapping the body reuses an open tab on another path', async () => {
  const w = loadWorker({ maxActions: 2, windows: ['https://rain.example.invalid/manage'] });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() {} }
  });
  assert.deepEqual(w.navigated.map((n) => n.to), ['/#l=tok']);
  assert.deepEqual(w.opened, []);
});

await test('a second warning does not open a second tab', async () => {
  // The regression: the same-path branch broke out of the loop so openWindow ran, and it matched
  // every warning after the first. Three warnings, three tabs.
  const w = loadWorker({ maxActions: 2, windows: ['https://rain.example.invalid/'] });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: { ...WARNING, url: '/#l=second' }, close() {} }
  });
  assert.deepEqual(w.opened, [], 'opened a new window instead of reusing the tab');
  assert.deepEqual(w.navigated.map((n) => n.to), ['/#l=second']);
});

await test('with no tab open a window is opened', async () => {
  const w = loadWorker({ maxActions: 2, windows: [] });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() {} }
  });
  assert.deepEqual(w.opened, ['/#l=tok']);
});

await test('a tab on another origin is ignored', async () => {
  const w = loadWorker({ maxActions: 2, windows: ['https://elsewhere.invalid/'] });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() {} }
  });
  assert.deepEqual(w.navigated, []);
  assert.deepEqual(w.opened, ['/#l=tok']);
});

await test('a tap on a leftover button opens the page and posts nothing', async () => {
  // A notification from before D-64, still in the tray. Its button's route is gone; the useful
  // answer is the notification's own page, the same as a tap on its body.
  const w = loadWorker({ maxActions: 2, windows: [] });
  await fire(w.listeners.notificationclick, {
    action: '0', notification: { data: OLD_WARNING, close() {} }
  });
  assert.deepEqual(w.fetches, []);
  assert.deepEqual(w.opened, ['/#l=tok']);
  assert.equal(w.shown.length, 0, 'no acknowledgement notification any more');
});

await test('a payload can choose which notifications it replaces', () => {
  // Nothing to assert against a browser here - the point is that the worker honours the sender's
  // tag rather than hard-coding one, which is what lets a settings link and a warning coexist.
  const w = loadWorker({ maxActions: 2 });
  return fire(w.listeners.push, { data: { json: () => ({ ...WARNING, tag: 'rainalert-alert' }) } })
    .then(() => {
      assert.equal(w.shown[0].options.tag, 'rainalert-alert');
    });
});

await test('a payload with no tag falls back to the single old one', async () => {
  const w = loadWorker({ maxActions: 2 });
  await fire(w.listeners.push, { data: { json: () => WARNING } });
  assert.equal(w.shown[0].options.tag, 'rainalert');
});

await test('what the push handler stores is what the click handler reads', async () => {
  /* The seam. Every other click case hand-feeds `notification.data`, so nothing asserted that the
     push handler ever stores it - drop `data: data` from showNotification, or rename a field in
     payload_for, and the whole suite stayed green while a body tap opened the signup form instead
     of the map. Same bug class as the old "actions computed and never passed", one field over.

     So: fire a real push, take the data off the notification it produced, and click *that*. */
  const w = loadWorker({ maxActions: 2, windows: [] });
  await fire(w.listeners.push, { data: { json: () => WARNING } });
  const stored = w.shown[0].options.data;
  assert.ok(stored, 'the push handler must store the payload on the notification');

  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: stored, close() {} }
  });
  assert.deepEqual(w.opened, ['/#l=tok'], 'a body tap must open where the payload pointed');
});

await test('an uncontrolled tab that refuses navigation still gets a window', async () => {
  // navigate() rejects for a client this worker has not claimed, which includeUncontrolled invites
  // into the list. Without the catch the tap silently does nothing.
  const w = loadWorker({ maxActions: 2, windows: ['https://rain.example.invalid/manage'],
                         navigateFails: true });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() {} }
  });
  assert.deepEqual(w.opened, ['/#l=tok']);
});

await test('tabs this worker has not claimed are still considered', async () => {
  const w = loadWorker({ maxActions: 2, windows: ['https://rain.example.invalid/manage'] });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() {} }
  });
  assert.equal(w.matchAllOptions.length, 1);
  assert.equal(w.matchAllOptions[0].type, 'window');
  assert.equal(w.matchAllOptions[0].includeUncontrolled, true,
    'a tab opened before this worker was claimed is still the reader\'s tab');
});

await test('a tapped notification is dismissed', async () => {
  // Without close() the warning stays in the shade after the reader has acted on it, so the next
  // one arrives under the same tag and replaces a notification they thought they had dealt with.
  const w = loadWorker({ maxActions: 2, windows: [] });
  let closed = false;
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() { closed = true; } }
  });
  assert.equal(closed, true, 'notificationclick must close the notification it handled');
});

await test('the tab is focused before it is navigated', async () => {
  // Chrome allows focus() only briefly after the click; navigate() resolves only once the page
  // has loaded. Navigate-then-focus lost the race whenever loading was slow, and the click
  // appeared to do nothing.
  const w = loadWorker({ maxActions: 2, windows: ['https://rain.example.invalid/manage'] });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() {} }
  });
  assert.deepEqual(w.calls, ['focus', 'navigate']);
  assert.deepEqual(w.opened, []);
});

await test('a refused focus still gets the page open', async () => {
  const w = loadWorker({ maxActions: 2, focusFails: true,
    windows: ['https://rain.example.invalid/manage'] });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() {} }
  });
  assert.deepEqual(w.opened, ['/#l=tok']);
});

await test('a client that cannot be focused is skipped, not crashed on', async () => {
  // `matchAll` can return a client with no focus(); calling it would throw inside waitUntil and the
  // tap would do nothing at all.
  const w = loadWorker({ maxActions: 2,
    windows: [{ url: 'https://rain.example.invalid/manage', focusable: false }] });
  await fire(w.listeners.notificationclick, {
    action: '', notification: { data: WARNING, close() {} }
  });
  assert.deepEqual(w.opened, ['/#l=tok'], 'an unfocusable client must fall through to a window');
  assert.deepEqual(w.navigated, []);
});

let failed = 0;
for (const [status, name, message] of results) {
  if (status === 'FAIL') { failed++; console.log(`FAIL ${name}: ${message}`); }
  else { console.log(`ok   ${name}`); }
}
console.log(failed ? `\n${failed} of ${results.length} failed` : `\nall ${results.length} passed`);
process.exit(failed ? 1 : 0);

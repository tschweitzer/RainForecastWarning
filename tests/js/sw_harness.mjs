/* A minimal service-worker environment, enough to run rainalert/api/static/sw.js for real.
 *
 * Why this exists: every test that named sw.js used to assert that a substring appeared in the
 * file. Those tests passed while `actions` was computed and never passed to showNotification, while
 * `maxActions === 0` was silently turned into 2, and while the tab-reuse branch opened a new window
 * for every warning after the first. A grep cannot see behaviour. This can.
 *
 * Reads the worker source, evaluates it against a fake `self`, and returns handles to the
 * registered listeners plus a log of what the worker did.
 */
import fs from 'node:fs';

export function loadWorker(options = {}) {
  const source = fs.readFileSync(options.path || 'rainalert/api/static/sw.js', 'utf8');
  const listeners = {};
  const shown = [];
  const opened = [];
  const navigated = [];
  const focused = [];
  /* Every navigate() and focus() in the order they were called (focusOrOpen must focus first). */
  const calls = [];
  /* Messages the worker posted to open pages, as { url: page, data }. */
  const posted = [];
  const fetches = [];
  const matchAllOptions = [];

  const scope = options.scope || 'https://rain.example.invalid/';
  /* A window may be given as a string, or as `{ url, focusable: false }` to model a client that
     does not expose focus() - `matchAll` can return those, and focusOrOpen guards for it. */
  const windows = (options.windows || []).map((w) => (typeof w === 'string' ? { url: w } : w))
    .map(({ url, focusable = true }) => {
    const client = {
    url,
    navigate(to) {
      navigated.push({ from: this.url, to });
      calls.push('navigate');
      /* A real WindowClient.navigate() rejects with a TypeError when the client is not controlled
         by this worker - which `includeUncontrolled: true` invites into the list - so the
         `.catch(... openWindow)` fallback in focusOrOpen is a live path, not a theoretical one.
         `navigateFails` was declared here and never honoured, which made that fallback untestable
         and the option a lie. */
      if (options.navigateFails) {
        return Promise.reject(new TypeError('cannot navigate an uncontrolled client'));
      }
      this.url = new URL(to, scope).href;
      return Promise.resolve(this);
    },
    postMessage(data) { posted.push({ url: this.url, data }); }
    };
    if (focusable) {
      client.focus = function () {
        focused.push(this.url);
        calls.push('focus');
        if (options.focusFails) { return Promise.reject(new Error('Not allowed to focus a window')); }
        return Promise.resolve(this);
      };
    }
    return client;
  });

  const self = {
    addEventListener: (name, fn) => { listeners[name] = fn; },
    skipWaiting: () => {},
    registration: {
      scope,
      showNotification: (title, opts) => { shown.push({ title, options: opts }); return Promise.resolve(); }
    },
    clients: {
      claim: () => Promise.resolve(),
      /* The options are recorded rather than ignored, so a test can assert them. `includeUncontrolled`
         in particular decides whether a tab this worker has not claimed is reusable at all. */
      matchAll: (opts) => { matchAllOptions.push(opts); return Promise.resolve(windows); },
      openWindow: (url) => { opened.push(url); return Promise.resolve({ url }); }
    }
  };

  const Notification = {};
  if ('maxActions' in options) Notification.maxActions = options.maxActions;

  const fetchImpl = (url, init) => {
    fetches.push({ url, init });
    if (options.fetchFails) return Promise.reject(new Error('offline'));
    return Promise.resolve({ ok: options.fetchOk !== false });
  };

  // `self` is also the global inside a worker, so the source's bare `Notification` and `fetch`
  // resolve through the same object. A Function wrapper gives us that without a real worker.
  const run = new Function('self', 'Notification', 'fetch', 'URL', `'use strict';\n${source}`);
  run(self, Notification, fetchImpl, URL);

  return { listeners, shown, opened, navigated, focused, calls, posted, fetches, matchAllOptions, windows, self };
}

/** Fire an event and wait for whatever the handler passed to waitUntil. */
export async function fire(listener, event) {
  const waits = [];
  await listener({ ...event, waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
  return waits;
}

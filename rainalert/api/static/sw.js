/* The service worker. Served from / (not /static/) so its scope covers the whole site - see the
   route in app.py for why that matters.

   Three jobs, and nothing else. This file runs in a worker with no DOM, it is the only code that
   runs when the site is closed, and a thrown exception here is a notification that never appears
   with no page to report it on. So: no dependencies, no caching, no offline shell. It is a
   notification handler, not a progressive web app framework.

   The payload it receives is built by notify/webpush.py:payload_for. The two have to agree, and
   that agreement is asserted in tests/test_webpush.py rather than left to comments. */

'use strict';

/* Chrome requires a notification to be shown for every push it delivers. Skip one and it shows
   "This site has been updated in the background" on your behalf, which is worse than anything we
   would have written. So every path here ends in showNotification, including the failure paths. */
var FALLBACK_TITLE = 'Regenwarnung';
/* Used when a payload names no tag. */
var FALLBACK_TAG = 'rainalert';
/* PNG, not the SVG the manifest uses: Chrome renders no SVG in a notification, on desktop or on
   Android, so an SVG here means the warning shows Chrome's own default icon instead of ours. The
   badge is separate because it is specified as a monochrome alpha mask at roughly 24px * DPR - a
   full-colour app icon there renders as a solid blob even where SVG works. */
var ICON = '/static/icon-192.png';
var BADGE = '/static/badge-96.png';

self.addEventListener('install', function () {
  /* Take over from the previous worker immediately rather than waiting for every tab to close.
     A subscriber with a stale worker is a subscriber not being warned, and they have no way to
     know they should close a tab to fix it. */
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    /* A payload we cannot parse is our bug, not the reader's, and they still get something rather
       than Chrome's generic text. */
    data = {};
  }

  event.waitUntil(Promise.all([
    handOver(data.url),
    self.registration.showNotification(data.title || FALLBACK_TITLE, {
      body: data.body || '',
      icon: ICON,
      badge: BADGE,
      lang: 'de',
      /* Everything the click handler needs, because it gets the notification and not the push. */
      data: data,
      /* No `actions`: notifications carry no buttons (DESIGN.md D-64). A tap opens `data.url`;
         settings and unsubscribing live on the settings page only. */
      /* One rain warning at a time: a second replaces the first rather than stacking. Without a
         tag, a shower that keeps re-triggering leaves a column of near-identical notifications
         and the reader stops reading any of them.

         From the payload, because a single tag for every kind of message meant a settings link, or
         this worker's own acknowledgement, replaced a live warning and its map link at the moment
         the reader wanted it. Falls back to the old single tag when the sender does not say. */
      tag: data.tag || FALLBACK_TAG,
      renotify: true,
      requireInteraction: false
    })
  ]));
});

/* A confirmation does not wait for its notification to be clicked (D-65).

   Desktop Chrome on a Mac showed why: macOS kept the confirmation in Notification Center while
   Chrome had already dropped it - `getNotifications()` came back empty - so clicking it reached
   nothing, and the reader could not finish signing up at all. Nothing here can make that click
   arrive. What this worker does have is the confirmation link itself, the moment the push lands.

   So it hands it on: kept for this origin in IndexedDB (`rainalert-sw` / `pending`), and posted to
   every open page of the site. The start page, open or opened later, takes it and goes to
   /confirm, which confirms as a tap on the notification would - same link, same proof, same
   browser. The link is single-use and expires with its token; it never leaves this browser. Only a
   confirmation link (`/confirm#a=`) on this worker's own origin is handed over, nothing else, and
   a failure here never costs the notification: this promise always resolves. */
var CONFIRM_PATH = '/confirm#a=';

function handOver(url) {
  if (typeof url !== 'string' || url.indexOf(self.registration.scope.replace(/\/$/, '') + CONFIRM_PATH) !== 0) {
    return Promise.resolve();
  }
  return keepPending(url)
    .catch(function () { /* the message below still reaches an open page */ })
    .then(function () {
      return self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    })
    .then(function (windows) {
      windows.forEach(function (client) {
        if (typeof client.postMessage === 'function') {
          client.postMessage({ type: 'rainalert-confirm', url: url });
        }
      });
    })
    .catch(function () { /* never at the notification's expense */ });
}

function keepPending(url) {
  if (typeof indexedDB === 'undefined') { return Promise.resolve(); }
  return new Promise(function (resolve, reject) {
    var open = indexedDB.open('rainalert-sw', 1);
    open.onupgradeneeded = function () { open.result.createObjectStore('pending'); };
    open.onerror = function () { reject(open.error); };
    open.onsuccess = function () {
      var tx = open.result.transaction('pending', 'readwrite');
      tx.objectStore('pending').put({ url: url, at: Date.now() }, 'confirm');
      tx.oncomplete = function () { open.result.close(); resolve(); };
      tx.onerror = function () { reject(tx.error); };
    };
  });
}

self.addEventListener('notificationclick', function (event) {
  var data = event.notification.data || {};
  event.notification.close();

  /* A tap opens where the message points - and so does a tap on a button. Notifications carry no
     buttons any more (D-64), but ones shown before that change can still be in the tray, with an
     "Einstellungen" button whose request token points at a route that no longer exists.
     `event.action` is set for those; ignoring it opens the notification's own page instead
     (a warning's map, the liveness message's settings page), which is the useful answer. */
  event.waitUntil(focusOrOpen(data.url || '/'));
});

/* No `pushsubscriptionchange` handler, deliberately.
 *
 * A browser can rotate an endpoint, and when it does the old one starts answering 410 and we delete
 * a subscriber who never unsubscribed. This event is the notice. There was a handler here that
 * re-subscribed and POSTed the new endpoint to `/api/v1/push/resubscribe`, and it was removed for
 * two reasons that compound.
 *
 * The endpoint it would have authenticated with is not a secret that proves anything. Knowing
 * somebody's endpoint does not let a third party push to them - the push service rejects a send
 * whose VAPID signature does not match the key the subscription was made with, so only we can push.
 * That makes an endpoint more like a username than a password, and an API that moves a subscription
 * on the strength of one is an API that redirects a stranger's warnings to an endpoint of your
 * choosing. Those warnings carry a locate reference (and then carried a settings token), so the
 * endpoint handed over a home address, which is the single thing this service is built not to leak.
 *
 * And it would have worked rarely. Firefox fires this event with neither `oldSubscription` nor
 * `newSubscription` populated, and Chrome only started populating them recently; without
 * `oldSubscription.options.applicationServerKey` there is no way for this worker to learn the key
 * it must re-subscribe with, so the handler returned without doing anything in exactly the cases it
 * existed for.
 *
 * What happens instead, and it is the whole of it: the next send to the rotated endpoint gets 410,
 * the subscriber row is deleted (dispatcher.deliver_queued, or the weekly liveness run), and the
 * reader subscribes again - losing their settings, which is the cost D-47 already states and
 * accepts. A bearer-string API that leaks a location is not a good trade for avoiding that.
 *
 * Note there is no third defence. An earlier version of this comment claimed "the page also
 * re-POSTs its subscription on every load"; nothing does. index.html POSTs only from the submit
 * handler, and manage.html only asks for a settings link.
 */

function focusOrOpen(url) {
  /* Reuse a tab that is already on the site rather than opening a third copy of it - but only when
     doing so actually loads the target.
     
     The trap: signup.js reads its `#l=` token in a load-time script and then replaceState's the
     hash away, so an open tab's URL is plain `/`. Navigating that tab to `/#l=<new token>` differs
     only in the fragment, which is a *same-document* navigation - no script re-runs, the new token
     is never read, and the reader taps their second warning and gets the country view or the stale
     view from the first one. Verified in Chromium: one locate call for two navigations.
     
     So a tab is only navigated when the path differs. Same path, different fragment, gets a fresh
     window instead, which does run the script. */
  return self.clients
    .matchAll({ type: 'window', includeUncontrolled: true })
    .then(function (windows) {
      for (var i = 0; i < windows.length; i++) {
        var client = windows[i];
        if (client.url.indexOf(self.registration.scope) !== 0 || !('focus' in client)) {
          continue;
        }
        /* No same-path special case any more, and this is the interesting part.
        
           There used to be one: the page read its `#l=` token once at load and replaceState'd the
           hash away, so navigating an open `/` tab to `/#l=<new token>` was a same-document
           navigation - no script re-ran, the token was never read, and the reader got the stale
           view. The branch here broke out of the loop so `openWindow` ran instead.
        
           signup.js now listens for `hashchange` and re-reads the token, so `navigate()` works. With
           both halves in place the branch had become not just redundant but harmful: it matched on
           *every* warning after the first, so each one opened another tab - warning 3 of an
           afternoon shower left three copies of the map open. Verified in node: two navigations to
           the same path with different hashes opened two windows.
        
           One fix, on the page side, where the token is actually read.

           Focus first, then navigate. Chrome lets a service worker focus a window only for a short
           moment after the click, and navigate() resolves only once the new page has loaded. It
           used to be navigate-then-focus, so whenever the page took longer than that moment the
           tab loaded in the background, focus() was refused, and the click looked like it did
           nothing - "sometimes works" on desktop Chrome on a Mac, reported from the field while
           confirming a signup. */
        return client.focus().then(function (focused) {
          return (focused || client).navigate(url);
        });
      }
      return self.clients.openWindow(url);
    })
    .catch(function () {
      return self.clients.openWindow(url);
    });
}

/* The confirmation link the service worker kept for this browser (sw.js `handOver`, D-65).

   A push confirmation used to complete only when its notification was clicked, and on desktop
   Chrome on a Mac that click can reach nothing at all. The worker now keeps the link it received
   and posts it to open pages; this script lets a page take it - once - and go confirm.

   `take()` resolves to the link and deletes it, or to null. Only a fresh link (younger than the
   token it carries can live) to this origin's /confirm is ever returned. `listen(fn)` calls `fn`
   with the message type when the worker posts while the page is open: 'rainalert-confirm' for a
   link to take, 'rainalert-confirmed' when the worker confirmed by itself (D-66). `clear()` forgets it and closes the
   confirmation notification, so a later click cannot open an already spent link. */
(function () {
  'use strict';

  var DB = 'rainalert-sw';
  var STORE = 'pending';
  var KEY = 'confirm';
  /* confirm_token_ttl_hours is 24; a link older than that cannot confirm anything. */
  var MAX_AGE_MS = 24 * 60 * 60 * 1000;
  var PREFIX = window.location.origin + '/confirm#a=';

  function withStore(mode, use) {
    return new Promise(function (resolve) {
      if (!window.indexedDB) { resolve(null); return; }
      var open;
      try {
        open = window.indexedDB.open(DB, 1);
      } catch (error) {
        resolve(null);
        return;
      }
      /* Bounded, like devicekey.js: a profile whose IndexedDB never answers must not hold up the
         page. */
      window.setTimeout(function () { resolve(null); }, 3000);
      open.onupgradeneeded = function () { open.result.createObjectStore(STORE); };
      open.onerror = function () { resolve(null); };
      open.onsuccess = function () {
        var result = null;
        var tx = open.result.transaction(STORE, mode);
        use(tx.objectStore(STORE), function (value) { result = value; });
        tx.oncomplete = function () { open.result.close(); resolve(result); };
        tx.onerror = function () { resolve(null); };
        tx.onabort = function () { resolve(null); };
      };
    });
  }

  function take() {
    return withStore('readwrite', function (store, answer) {
      var read = store.get(KEY);
      read.onsuccess = function () {
        var entry = read.result;
        if (!entry) { return; }
        store.delete(KEY);
        var fresh = typeof entry.at === 'number' && Date.now() - entry.at < MAX_AGE_MS;
        if (fresh && typeof entry.url === 'string' && entry.url.indexOf(PREFIX) === 0) {
          answer(entry.url);
        }
      };
    });
  }

  function clear() {
    withStore('readwrite', function (store) { store.delete(KEY); });
    if (!('serviceWorker' in navigator)) { return; }
    navigator.serviceWorker.getRegistration('/').then(function (registration) {
      if (!registration || !registration.getNotifications) { return null; }
      return registration.getNotifications().then(function (shown) {
        shown.forEach(function (notification) {
          var url = notification.data && notification.data.url;
          if (typeof url === 'string' && url.indexOf(PREFIX) === 0) { notification.close(); }
        });
      });
    }).catch(function () { /* nothing to tidy */ });
  }

  function listen(handler) {
    if (!('serviceWorker' in navigator)) { return; }
    navigator.serviceWorker.addEventListener('message', function (event) {
      var type = event.data && event.data.type;
      if (type === 'rainalert-confirm' || type === 'rainalert-confirmed') { handler(type); }
    });
  }

  window.RainPending = { take: take, clear: clear, listen: listen };
})();

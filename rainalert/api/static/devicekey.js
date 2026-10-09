/* Device keys (DESIGN.md D-64, docs/PLAN_DEVICE_KEY.md).

   A push subscriber's browser keeps a non-extractable ECDSA P-256 key in IndexedDB and signs every
   request the settings page makes with it. The server keeps the public half. There is no session,
   no cookie and no CSRF value for these requests: each one is authenticated by its own signature
   (rainalert/devicekeys.py has the server half and the message format).

   The reader never sees any of this. To them they are subscribed or not; the settings simply open.
   So nothing here produces text. Every failure means "no key", and the page falls back to the
   ordinary link, which registers a fresh key.

   Where the key comes from: only ever together with a push-delivered single-use token - the
   confirmation, or a settings link - redeemed in this browser (`prepareRedemption`). Never at
   subscribe time and never from an authenticated request (§4.1).

   Storage: IndexedDB, database `rainalert`, store `device`, two slots: `pending` (generated, sent
   with a redemption, not yet confirmed by the server) and `active`. Without usable IndexedDB the
   key lives in memory for this tab only and is gone when it closes (§4.1). */
(function () {
  'use strict';

  /* The `client` value the server uses to tell this script from a page that predates it. */
  var CLIENT = 'dk1';
  var PREFIX = 'rainalert-request-v1';
  var DB_NAME = 'rainalert';
  var STORE = 'device';
  var DAY_MS = 24 * 60 * 60 * 1000;

  var memory = {};          // slot -> record, when IndexedDB cannot be used
  var useMemory = false;
  var dbPromise = null;
  var offset = null;        // server seconds minus performance.now() seconds

  function supported() {
    return !!(window.crypto && window.crypto.subtle && window.TextEncoder);
  }

  function b64url(bytes) {
    var text = '';
    for (var i = 0; i < bytes.length; i++) { text += String.fromCharCode(bytes[i]); }
    return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  // ---- storage ---------------------------------------------------------------------------------
  function database() {
    if (useMemory || !window.indexedDB) { return Promise.resolve(null); }
    if (!dbPromise) {
      dbPromise = new Promise(function (resolve) {
        var request;
        try {
          request = window.indexedDB.open(DB_NAME, 1);
        } catch (error) {
          resolve(null);
          return;
        }
        request.onupgradeneeded = function () { request.result.createObjectStore(STORE); };
        request.onsuccess = function () { resolve(request.result); };
        request.onerror = function () { resolve(null); };
        request.onblocked = function () { resolve(null); };
      }).then(function (db) {
        if (!db) { useMemory = true; }
        return db;
      });
    }
    return dbPromise;
  }

  /* One read-modify-write of a slot, atomic: `decide(current)` returns the record to store, `null`
     to delete, or `undefined` to leave it. Resolves to whether anything changed. A CryptoKey that
     cannot be stored (DataCloneError on some engines) switches this tab to memory. */
  function transact(slot, decide) {
    return database().then(function (db) {
      if (!db) {
        var next = decide(memory[slot] || null);
        if (next === undefined) { return false; }
        if (next === null) { delete memory[slot]; } else { memory[slot] = next; }
        return true;
      }
      return new Promise(function (resolve, reject) {
        var changed = false;
        var tx;
        try {
          tx = db.transaction(STORE, 'readwrite');
        } catch (error) {
          reject(error);
          return;
        }
        var store = tx.objectStore(STORE);
        var read = store.get(slot);
        read.onsuccess = function () {
          var next = decide(read.result || null);
          if (next === undefined) { return; }
          try {
            if (next === null) { store.delete(slot); } else { store.put(next, slot); }
            changed = true;
          } catch (error) {
            tx.abort();
            reject(error);
          }
        };
        tx.oncomplete = function () { resolve(changed); };
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error); };
      });
    }).catch(function () {
      /* IndexedDB refused the record or the transaction: carry on in memory for this tab. */
      useMemory = true;
      return transact(slot, decide);
    });
  }

  function read(slot) {
    var found = null;
    return transact(slot, function (current) { found = current; return undefined; })
      .then(function () { return found; });
  }

  // ---- keys --------------------------------------------------------------------------------------
  function generate() {
    var pair;
    return window.crypto.subtle.generateKey(
      { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']
    ).then(function (generated) {
      pair = generated;
      /* The public half of a non-extractable pair is always exportable. */
      return window.crypto.subtle.exportKey('spki', pair.publicKey);
    }).then(function (spki) {
      var bytes = new Uint8Array(spki);
      return window.crypto.subtle.digest('SHA-256', bytes).then(function (hash) {
        return {
          privateKey: pair.privateKey,
          spki: b64url(bytes),
          keyId: b64url(new Uint8Array(hash))
        };
      });
    });
  }

  function load() {
    if (!supported()) { return Promise.resolve(null); }
    return read('active').catch(function () { return null; });
  }

  /* Delete the active key only if it is still the one the server refused - another tab may have
     replaced it a moment ago (review 2, two tabs). */
  function forget(keyId) {
    return transact('active', function (current) {
      return current && current.keyId === keyId ? null : undefined;
    }).catch(function () { return false; });
  }

  function forgetAll() {
    return transact('active', function () { return null; })
      .then(function () { return transact('pending', function () { return null; }); })
      .catch(function () { return false; });
  }

  // ---- time ----------------------------------------------------------------------------------------
  /* Signed with the server's clock, not the phone's (§4.3): seeded from the time the page was
     rendered, refreshed from every response's Date header. */
  function syncFromPage(serverSeconds) {
    if (typeof serverSeconds === 'number' && isFinite(serverSeconds)) {
      offset = serverSeconds - performance.now() / 1000;
    }
  }

  function syncFromResponse(response) {
    var date = response && response.headers && response.headers.get('Date');
    var ms = date ? Date.parse(date) : NaN;
    if (!isNaN(ms)) { offset = ms / 1000 + 0.5 - performance.now() / 1000; }
  }

  function serverNow() {
    var seconds = offset === null ? Date.now() / 1000 : offset + performance.now() / 1000;
    return Math.floor(seconds);
  }

  // ---- signing ---------------------------------------------------------------------------------
  function authorization(record, method, path, bodyText) {
    var encoder = new TextEncoder();
    var t = serverNow();
    return window.crypto.subtle.digest('SHA-256', encoder.encode(bodyText)).then(function (hash) {
      var message = [
        PREFIX, window.location.origin, method.toUpperCase(), path, String(t),
        b64url(new Uint8Array(hash))
      ].join('\n');
      return window.crypto.subtle.sign(
        { name: 'ECDSA', hash: 'SHA-256' }, record.privateKey, encoder.encode(message)
      );
    }).then(function (signature) {
      return 'RainKey key=' + record.keyId + ', t=' + t + ', sig='
        + b64url(new Uint8Array(signature));
    });
  }

  function refusal(response) {
    return response.clone().json().then(function (body) {
      return body && body.detail && body.detail.error;
    }).catch(function () { return null; });
  }

  /* A request to the settings API, signed. Resolves to the Response, or to null when this browser
     holds no key. A `clock` refusal resyncs and retries once; an `unknown_key` refusal first checks
     whether another tab has just replaced the key, and only then forgets it (compare-and-delete). */
  function signedFetch(method, path, body) {
    var bodyText = body === undefined ? '' : JSON.stringify(body);
    return load().then(function (record) {
      if (!record) { return null; }
      var attempts = 0;
      function attempt() {
        attempts++;
        return authorization(record, method, path, bodyText).then(function (header) {
          var headers = { Authorization: header };
          if (bodyText) { headers['Content-Type'] = 'application/json'; }
          return fetch(path, {
            method: method,
            headers: headers,
            body: bodyText || undefined,
            /* Nothing ambient rides along: the signature is the whole credential. */
            credentials: 'omit'
          });
        }).then(function (response) {
          syncFromResponse(response);
          if (response.status !== 401 || attempts >= 3) { return response; }
          return refusal(response).then(function (reason) {
            if (reason === 'clock') { return attempt(); }
            if (reason !== 'unknown_key') { return response; }
            return load().then(function (again) {
              if (again && again.keyId !== record.keyId) {
                record = again;
                return attempt();
              }
              return forget(record.keyId).then(function () { return response; });
            });
          });
        });
      }
      return attempt();
    }).catch(function () { return null; });
  }

  // ---- registering --------------------------------------------------------------------------------
  function pushSubscription() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      return Promise.resolve(null);
    }
    return navigator.serviceWorker.getRegistration('/').then(function (registration) {
      return registration ? registration.pushManager.getSubscription() : null;
    }).catch(function () { return null; });
  }

  /* The fields a page sends beside a push-delivered token: proof that this browser holds the
     subscription (§4.1), and a fresh public key, stored here as `pending` until the server says it
     was registered. `persistentOnly`: the confirm page navigates away on submit, so a key that
     could only live in memory would be gone before it was ever used - it sends none instead. */
  function prepareRedemption(options) {
    var fields = { client: CLIENT, endpoint: '', p256dh: '', device_key: '' };
    return pushSubscription().then(function (subscription) {
      if (subscription) {
        fields.endpoint = subscription.endpoint || '';
        var key = subscription.getKey && subscription.getKey('p256dh');
        if (key) { fields.p256dh = b64url(new Uint8Array(key)); }
      }
      if (!supported()) { return fields; }
      return generate().then(function (fresh) {
        return transact('pending', function () {
          return { keyId: fresh.keyId, privateKey: fresh.privateKey };
        }).then(function () {
          if (options && options.persistentOnly && useMemory) {
            return transact('pending', function () { return null; });
          }
          fields.device_key = fresh.spki;
        });
      }).then(function () { return fields; }, function () { return fields; });
    }).catch(function () { return fields; });
  }

  /* The server registered `keyId`: promote the pending key, if it is still that one. */
  function activate(keyId) {
    var promoted = null;
    return transact('pending', function (pending) {
      if (!pending || pending.keyId !== keyId) { return undefined; }
      promoted = pending;
      return null;
    }).then(function () {
      if (!promoted) { return false; }
      return transact('active', function () {
        return { keyId: promoted.keyId, privateKey: promoted.privateKey, rotatedAt: Date.now() };
      });
    }).catch(function () { return false; });
  }

  function dropPending() {
    return transact('pending', function () { return null; }).catch(function () { return false; });
  }

  /* At most once a day, replace the key with a fresh one, signed by the current key (§4.5). A key
     planted by injected script dies the next time the reader opens their settings. Not for a key
     that lives only in this tab's memory: it dies with the tab anyway. */
  function rotateIfDue() {
    var record;
    return load().then(function (current) {
      record = current;
      if (!record || useMemory) { return null; }
      if (record.rotatedAt && Date.now() - record.rotatedAt < DAY_MS) { return null; }
      return generate().then(function (fresh) {
        return signedFetch('POST', '/api/v1/device-key/rotate', { device_key: fresh.spki })
          .then(function (response) {
            if (!response || !response.ok) { return null; }
            return transact('active', function (active) {
              if (!active || active.keyId !== record.keyId) { return undefined; }
              return { keyId: fresh.keyId, privateKey: fresh.privateKey, rotatedAt: Date.now() };
            });
          });
      });
    }).catch(function () { return null; });
  }

  window.RainKey = {
    CLIENT: CLIENT,
    supported: supported,
    syncFromPage: syncFromPage,
    prepareRedemption: prepareRedemption,
    activate: activate,
    dropPending: dropPending,
    load: load,
    signedFetch: signedFetch,
    rotateIfDue: rotateIfDue,
    forgetAll: forgetAll
  };
})();

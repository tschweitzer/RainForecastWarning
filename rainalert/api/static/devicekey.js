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

  /* A page's `window`, or the service worker's `self`: sw.js imports this file too, to confirm a
     signup the moment its push arrives (D-66), and a worker has no `window`. */
  var G = typeof window !== 'undefined' ? window : self;

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
  var rotation = null;      // a rotation in flight; this tab's other requests wait for it

  function supported() {
    return !!(G.crypto && G.crypto.subtle && G.TextEncoder);
  }

  function b64url(bytes) {
    var text = '';
    for (var i = 0; i < bytes.length; i++) { text += String.fromCharCode(bytes[i]); }
    return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  // ---- storage ---------------------------------------------------------------------------------
  function database() {
    if (useMemory || !G.indexedDB) { return Promise.resolve(null); }
    if (!dbPromise) {
      dbPromise = new Promise(function (resolve) {
        /* Bounded: in some private modes and stuck profiles `open` never fires any event, and a
           page waiting on it - the confirm page holds its submit until the proof is ready - would
           wait forever (code review). After three seconds this tab carries on in memory. */
        G.setTimeout(function () { resolve(null); }, 3000);
        var request;
        try {
          request = G.indexedDB.open(DB_NAME, 1);
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
    return G.crypto.subtle.generateKey(
      { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']
    ).then(function (generated) {
      pair = generated;
      /* The public half of a non-extractable pair is always exportable. */
      return G.crypto.subtle.exportKey('spki', pair.publicKey);
    }).then(function (spki) {
      var bytes = new Uint8Array(spki);
      return G.crypto.subtle.digest('SHA-256', bytes).then(function (hash) {
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
    return G.crypto.subtle.digest('SHA-256', encoder.encode(bodyText)).then(function (hash) {
      var message = [
        PREFIX, G.location.origin, method.toUpperCase(), path, String(t),
        b64url(new Uint8Array(hash))
      ].join('\n');
      return G.crypto.subtle.sign(
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

  /* One signed request with `record`'s key, retried once on a `clock` refusal (resynced from that
     answer's Date header). With `recover`, an `unknown_key` refusal first checks whether another tab
     has just replaced the key and retries with the new one, and otherwise forgets this key
     (compare-and-delete). Without it - for the rotation, which must be signed by exactly the key it
     replaces - the refusal is simply returned (code review: a rotation that recovered could leave
     the server and this browser holding different keys). */
  function send(record, method, path, bodyText, recover) {
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
          if (reason === 'clock' && attempts === 1) { return attempt(); }
          if (reason !== 'unknown_key' || !recover) { return response; }
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
  }

  /* A request to the settings API, signed. Resolves to the Response, or to null when this browser
     holds no key. Waits for a rotation this tab has in flight, so it never signs with a key the
     server has just replaced. */
  function signedFetch(method, path, body) {
    var bodyText = body === undefined ? '' : JSON.stringify(body);
    return (rotation || Promise.resolve()).then(load).then(function (record) {
      if (!record) { return null; }
      return send(record, method, path, bodyText, true);
    }).catch(function () { return null; });
  }

  // ---- registering --------------------------------------------------------------------------------
  function pushSubscription() {
    /* In the service worker, its own registration; on a page, the one registered at '/'. */
    if (G.registration && G.registration.pushManager) {
      return G.registration.pushManager.getSubscription().catch(function () { return null; });
    }
    if (!('serviceWorker' in navigator) || !('PushManager' in G)) {
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
    /* Bounded: a page waiting on this must not wait forever on a service worker that never
       answers. Without an answer the fields stay empty and the server says so. */
    var subscription = Promise.race([
      pushSubscription(),
      new Promise(function (resolve) { G.setTimeout(function () { resolve(null); }, 5000); })
    ]);
    return subscription.then(function (subscription) {
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

  /* The server registered `keyId`: promote the pending key, if it is still that one - in one
     transaction, so leaving the page halfway cannot lose it (code review). */
  function activate(keyId) {
    function promoted(pending) {
      return { keyId: pending.keyId, privateKey: pending.privateKey, rotatedAt: Date.now() };
    }
    return database().then(function (db) {
      if (!db) {
        if (!memory.pending || memory.pending.keyId !== keyId) { return false; }
        memory.active = promoted(memory.pending);
        delete memory.pending;
        return true;
      }
      return new Promise(function (resolve, reject) {
        var done = false;
        var tx = db.transaction(STORE, 'readwrite');
        var store = tx.objectStore(STORE);
        var read = store.get('pending');
        read.onsuccess = function () {
          var pending = read.result;
          if (!pending || pending.keyId !== keyId) { return; }
          store.put(promoted(pending), 'active');
          store.delete('pending');
          done = true;
        };
        tx.oncomplete = function () { resolve(done); };
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error); };
      });
    }).catch(function () { return false; });
  }

  function dropPending() {
    return transact('pending', function () { return null; }).catch(function () { return false; });
  }

  /* At most once a day, replace the key with a fresh one, signed by the current key (§4.5). A key
     planted by injected script dies the next time the reader opens their settings. Not for a key
     that lives only in this tab's memory: it dies with the tab anyway.

     Signed with exactly the key being replaced, no recovery (see `send`), and this tab's other
     requests wait for it. Once the server has the fresh key it is stored unless another tab has
     meanwhile put a different one in place; if another tab *deleted* the old one - its request
     raced this rotation and met `unknown_key` - the fresh key is stored all the same, because it
     is the one the server holds (code review). */
  function rotateIfDue() {
    if (rotation) { return rotation; }
    rotation = load().then(function (record) {
      if (!record || useMemory) { return null; }
      if (record.rotatedAt && Date.now() - record.rotatedAt < DAY_MS) { return null; }
      return generate().then(function (fresh) {
        var body = JSON.stringify({ device_key: fresh.spki });
        return send(record, 'POST', '/api/v1/device-key/rotate', body, false)
          .then(function (response) {
            if (!response || !response.ok) { return null; }
            return transact('active', function (active) {
              if (active && active.keyId !== record.keyId) { return undefined; }
              return { keyId: fresh.keyId, privateKey: fresh.privateKey, rotatedAt: Date.now() };
            });
          });
      });
    }).catch(function () { return null; }).then(function (result) {
      rotation = null;
      return result;
    });
    return rotation;
  }

  G.RainKey = {
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

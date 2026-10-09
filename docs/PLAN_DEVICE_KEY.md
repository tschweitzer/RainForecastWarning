# Plan: a device key instead of push round trips and sessions

Status: **implemented 2026-10-09** (revision 3, with security review 3 folded in, §12). Where the
code differs from the text below, §14 says how and why. Scope: web push subscribers;
email keeps its magic link and session. Would become DESIGN.md D-64 once agreed (D-63 is the mail cap).

History: revision 1 replaced the push round trip with a key that opened the ordinary session
(security review 1, §8). Revision 2 dropped the session for key holders and signed every request
(§9; security review 2, §10). Revision 3 applies the product decisions and the infrastructure
check in §11. The most visible decision: the key is never mentioned to the user, and
notifications carry no buttons.

## 1. The problem

A push subscriber who opens `/manage` without a live session cookie (sessions last 30 minutes,
at most 120, D-25) gets there by a detour. The page sends its push endpoint to
`POST /api/v1/manage/link`, the server pushes a notification carrying a single-use, 15-minute
`#t=` link, and tapping it opens the session. The "Einstellungen" button on every warning and on
the liveness notification does the same from the service worker: it POSTs a durable request token
to `/api/v1/manage/request` (`sw.js`, `mail.py` `settings_action`), which pushes the link.

That detour is sound - only the subscribed browser can decrypt the push, so returning the link
proves possession of that browser - but it is the wrong tool for what it buys:

| | Today's push round trip |
|---|---|
| Stops someone holding the unlocked phone | No - the notification arrives on the same phone |
| Stops script injected into our origin | No - `registration.getNotifications()` exposes the token in `.data` |
| Stops a remote attacker who knows the endpoint | **Yes** - this is its real value (the endpoint is a username, not a password: DESIGN.md, the removed `/push/resubscribe`) |
| Leaves no long-lived credential to leak | **Yes** - sessions lapse within 2 h |
| Works when notifications are muted, in focus mode, delayed by Doze, or the push service hiccups | **No** - and the settings page is a push subscriber's only route to "Abmelden und meine Daten löschen" |
| Friction | Tap, wait for a push, tap the notification, wait for the page |
| Complexity it has cost so far | D-42 (spent links), D-49 (fragment-only navigation), the `start()` overlap guard, three rate-limit buckets, link-cancelling by strangers |

A *silent* proof over push is not available: Chrome and Safari require every push to show a
notification (`userVisibleOnly: true`).

## 2. The proposal in one paragraph

When a push subscriber proves possession of their browser the way they already do - by
confirming the subscription, or by redeeming a settings link that arrived as a push - the page
also registers a **non-extractable ECDSA P-256 key pair** created by WebCrypto and kept in
IndexedDB. From then on **every request the settings page makes is signed** with that key -
method, path, body and time - and the server authenticates each one on its own. There is no
session, no cookie, no CSRF value and nothing to expire for key holders. The user never sees any
of this: to them they are subscribed or not, the settings simply open, and the only account
controls are subscribing and unsubscribing. Notifications carry no buttons. The push link remains
only as the quiet fallback for a browser without a key.

## 3. Why a device key (option B)

The alternatives considered (from the discussion on 2026-10-09):

- **A. A long-lived device cookie** (`__Host-`, HttpOnly, SameSite=Strict, ~1 year, rotated).
  Simple, and lives exactly as long as the push subscription (both are site data; D-47). But it
  is a long-lived *bearer* credential: its bytes are the key. A HAR file sent with a bug report,
  a cookie exporter, a devtools screenshot - any of them hands over standing access to someone's
  home coordinates. A would be acceptable for this threat model. B is better only against those
  copy-the-bytes leaks (review 1, §8), and with signed requests it also needs no CSRF defence.
- **B. A non-extractable WebCrypto key** (this plan). Same lifetime as A, same "it is this
  browser" proof, but the private key cannot be read out by script, HAR files or cookie
  exporters - it can only be *used*, by our own origin, while the browser runs. What travels
  over the wire is a signature bound to one request and a two-minute window. Not better than A
  against malware: Chrome keeps IndexedDB key material in the profile without the app-bound
  encryption Windows cookies get, and a malicious extension can *use* the key as it can use a
  cookie. Whoever copies the profile owns the device anyway.
- **C. The push subscription's `auth` secret as a password.** It is readable by any script on
  the origin, it is stored in plaintext in our database because encryption needs it, and it
  would become a permanent password nobody can rotate without resubscribing. Rejected.
- **D. WebAuthn / passkeys.** The strongest hardware-backed option, and the one to reach for if
  this ever holds anything more sensitive. Rejected for now: every use shows a system prompt
  (biometric/PIN), and passkeys may sync to a Google or Apple account (no longer "this
  browser"). Both contradict "subscribed or not". The UX and code cost buys protection against
  an attacker - the one holding the unlocked phone - that the radar's threat model accepts.
- **Keep the round trip.** Safe; but see §1: it pays mostly in availability and friction.

Honest caveat: the longest-lived credential a push subscriber has today is not a session but
the API token the confirmation page shows them (`confirmed.html`); §4.9 removes it for push.

## 4. Design

### 4.1 Enrolment - the key rides in with an existing proof

No new proof is invented. A key is accepted **only together with** a push-delivered single-use
token being redeemed in the same request:

- **Confirmation** (`POST /confirm`, the `#a=` path that confirms on open, D-36): the confirm
  page generates the key pair and stores it as *pending* in IndexedDB before submitting. It adds
  hidden `device_key` (SPKI, base64url), `endpoint` and `p256dh` fields to the form.
- **Settings link** (`POST /api/v1/manage/session`, the `#t=` fallback path): `redeem()` sends
  `device_key`, `endpoint` and `p256dh` alongside `token`.

**The redeeming browser must hold the push subscription the token belongs to.** The server
redeems a webpush subscriber's token only if `hash_address('webpush', endpoint)` equals that
subscriber's `address_hash`, **or** the `p256dh` equals the stored `push_p256dh` (compared as decoded bytes: the stored value is
base64url text, while `getKey()` returns an ArrayBuffer). Either match is
equally safe against the attack below, because a victim's browser presents the victim's values,
which never match the attacker's subscriber. Accepting either keeps a browser that re-encodes or
migrates its endpoint string from being locked out (review 2).

This applies to every push token redemption, with or without a key. A push-delivered token can
only be opened in the browser holding that subscription, so a legitimate redemption always
passes. An attacker's own link sent to a victim always fails. That closes review 1's High finding
and also today's two-hour login CSRF.

Errors - and **neither spends the token** (review 3). Both checks run inside the service
*before* `used_at` is set or anything is committed (`confirm` and `redeem_manage_token`,
`subscriptions.py`). A test asserts that the token still works after a refused attempt.
Otherwise a reload or a retry would meet a spent token, and an unconfirmed signup would be
purged.
- **A mismatch** answers `{"error": "push_mismatch"}`. The page says, in subscription terms, that
  this link belongs to a subscription this browser no longer has, and offers to subscribe again.
  Each mismatch is logged as a counted event, so a systematic lockout shows up. A current script
  that cannot read its own subscription (an exception, no registration, a VAPID-key mismatch)
  sends what it has and gets this answer too.
- **An old script** is recognised by an explicit `client` version field that the new script
  always sends - **not** by a missing endpoint, which a current script can also produce. A request
  without the field answers `{"error": "stale_page"}`. That is a tab still running the previous
  script after a deploy (the service worker reuses open tabs, D-49), and the page reloads itself
  **at most once**, using a `sessionStorage` flag, so it can never loop.

Then:

- key present and valid (uncompressed P-256 SPKI, length capped) and the feature on → the key is
  stored, replacing any previous one (one key per subscriber), and **no cookie is set**. The
  answer says `{"enrolled": "<key_id>"}`; only then does the page mark its pending key active.
- no key (WebCrypto unavailable) or feature off → today's behaviour: the session cookie is set.

`key_id` is `base64url(SHA-256(SPKI))`, computed on both sides. A server-chosen id has no clean
way back to script on the confirm path, which is a top-level form navigation. The confirmed page
renders `data-key-enrolled="<key_id>"`. Promoting the pending key is a compare-and-set: it happens
only if the pending entry is still the one with that `key_id` (two tabs, §4.5).

If IndexedDB is unavailable but WebCrypto works, the key is still enrolled, and kept **in memory
for that tab only**. The next visit goes through the link fallback, which enrols a fresh key.

**Why not "any authenticated session or signed request may enrol a key"?** Because a credential
that leaks would then turn temporary access into permanent access. Tying enrolment to the
single-use push token keeps the key exactly as strong as the push round trip it replaces.

**Why not at subscribe time?** `POST /subscriptions` is unauthenticated. An attacker who knew a
victim's endpoint could subscribe it with *their own* key, and because a push confirmation
confirms on open (D-36), one tap by the victim would bind the attacker's key.

### 4.2 Signed requests

Every subscriber-scoped API call the settings page makes carries the header below:
`GET`, `PATCH` and `DELETE /api/v1/subscriptions/me`, and `PUT /api/v1/subscriptions/me/location`.

    Authorization: RainKey key=<key_id>, t=<unix seconds>, sig=<base64url P1363 signature>

It signs this message (UTF-8, `\n`-separated):

    rainalert-request-v1
    <origin from public_base_url, normalised to scheme://host[:port]>
    <METHOD>
    <path, exactly as received: scope["raw_path"], not the percent-decoded url.path>
    <t>
    <base64url(SHA-256(the body bytes actually received))>

No settings call has a query string, so a `RainKey` request that carries one is refused rather
than canonicalised. `t` must be digits only.

**The origin comes from `public_base_url` and never from the request.** Step 0 (§11) showed why
this matters: behind Firebase Hosting the app sees the Cloud Run hostname and plain `http`, not
`https://rainalerts.web.app`.

`current_subscriber` gains a third branch beside `Bearer` and the cookie. For a `RainKey` header
it:
1. looks up the key;
2. checks that `t` is within ±120 s of the server clock;
3. verifies the signature over the message it builds from **its own** origin and the request it
   actually received;
4. returns the key's subscriber.

Reading the body needs `await request.body()` while `current_subscriber` is synchronous, so the
hashing sits in a small async dependency and the database work stays synchronous.

A request with this header is authenticated by it alone. A failed check answers 401 with **no
fallback** to a cookie on the same request; cookies are ignored. No CSRF value is required:
that is D-27's rule for header credentials, which nothing cross-site can attach. Our API sends no
CORS headers, so another site cannot set `Authorization` on a request to us, and it could not sign
one anyway.

Every authenticated API response carries `Cache-Control: private, no-store`. Firebase Hosting's
CDN sits in front of the API (step 0 saw `x-cache: MISS`) and keys on the `__session` cookie,
which key-mode requests do not carry. If an authenticated GET were ever cacheable, every key
holder would share one cache entry - someone else's home coordinates (review 2). This applies to
API responses only. HTML pages keep their bfcache-friendly `private, no-cache`.

Signatures are 64 bytes raw `r||s` (IEEE P1363) from WebCrypto. The server rejects any other
length and converts with `encode_dss_signature`. Malleability is irrelevant: a mutated signature
authorises the same request. Verification is ~0.2 ms; the browser signs in ~1 ms.

Failures answer 401 with a reason the page acts on:
- `{"error": "unknown_key"}`: no such key. The page deletes its key (compare-and-delete, §4.5).
- `{"error": "clock"}`: outside the time window. The page resyncs and retries once (§4.3).
- anything else is a plain 401: the page keeps its key and uses the link fallback.

**The page deletes its key only on `unknown_key`.** A rollback, a traffic split or the kill
switch answer without that body and cannot wipe everyone's keys (review 1, Medium).

### 4.3 Time

Phone clocks drift, so the page does not trust its own; it signs with server time. The page
renders the server's clock (`data-server-time`) and keeps an offset to `performance.now()`,
refreshed from the `Date` header of every response (same-origin fetch can read `Date`).

On `{"error": "clock"}` the page takes the offset from that response and retries once - **every**
signed call, the DELETE included. `performance.now()` pauses while iOS and macOS devices sleep,
so the first request after waking will often get `clock`. A page restored from the back/forward
cache with an old render time heals the same way.

### 4.4 Replay

There is deliberately no per-request nonce store. A captured signed request can be replayed only
**unchanged** - same method, path and body - and only within two minutes. Capturing one requires
breaking TLS, running script in our origin (which can sign for itself anyway), or being handed a
HAR file within two minutes of its recording.

The worst case is an old PATCH replayed after a newer one, which reverts a setting - and only for
someone who already captured the request. A nonce table would cost a database write per request
to close a window this narrow. Review 2 agreed.

### 4.5 The settings page

`/manage` without a fragment: load the key from IndexedDB → signed `GET /api/v1/subscriptions/me`.

- **200** → the panel. No session countdown and no "Verlängern" button, because there is nothing
  to expire.
- **`unknown_key`** → first reload the key from IndexedDB and, if it changed, retry once:
  another tab may just have rotated it (below). Otherwise delete the key **only if the stored
  `key_id` is still the one that was refused** (compare-and-delete), then fall back to the link
  step, **without a message**. Without the compare, a tab still on the old key would delete the
  newer key another tab had just enrolled.
- **No key, or another failure** → the link step as today ("wir schicken dir einen Link"), which
  enrols a key. This is now the rare path: subscribers from before the change, browsers that
  cannot keep a key, possibly iOS (§12).

`#t=` links redeem and enrol as in §4.1, then continue as above.

**Silent rotation** (review 3). On an ordinary visit, at most once a day, the page generates a
fresh non-extractable key and replaces the current one with `POST /api/v1/device-key/rotate`,
signed by the current key and carrying the new SPKI. The new key is written to IndexedDB with
compare-and-set only after the server confirms. The user sees nothing.

This is what bounds a key planted by injected script (§7), without an expiry and without asking
the user for anything:
- if the planted key sits in the reader's own IndexedDB, the reader's next visit swaps in a key
  the attacker never saw, and the attacker's copy dies;
- if the attacker rotates first, the reader gets `unknown_key`, falls back to the link step and
  re-enrols, which evicts the attacker.

It adds no reach: whoever holds the current key already has standing access. Its cost is one
route and about 30 lines.

A small `signedFetch()` wrapper in `static/devicekey.js` replaces `fetch` for the authenticated
calls. In cookie mode (email) it is plain `fetch` with the CSRF header, as now.

**Account controls: subscribe and unsubscribe, nothing else.** "Abmelden und meine Daten löschen"
stays. "Sitzung auf diesem Gerät beenden" goes, **for every subscriber**:
- with a key it would do nothing, because the next visit signs in again;
- email sessions end after 30 minutes on their own.

The page never mentions a key, a session or quick access. The key's life is the subscription's:
it is created on confirmation, deleted server-side by the cascade on unsubscribe, and deleted in
the browser by the page right after a successful unsubscribe (next to the existing
`subscription.unsubscribe()`), or by clearing site data. `/api/v1/manage/logout` goes with the
button. The countdown and extend UI remain for email sessions only.

**Decided 2026-10-09: the same for push and email - no sign-out control for either.** Review 3
pointed out what this costs email subscribers on a shared computer: the next person has the
settings for whatever is left of the 30-minute session (sliding, 120 minutes at most). That cost
is accepted.

### 4.6 Notifications carry no buttons

Today two notifications carry an "Einstellungen" action: warnings and the liveness message
(`mail.py` `settings_action`, `liveness.py`). Both go.
- **Warnings:** tapping opens the map at the warned place (`#l=`), unchanged. Settings are one tap
  further via the site's own "Einstellungen" link, which with a key opens directly.
- **Liveness:** tapping already opens `/manage`, so with a key the settings page - unsubscribe
  included - opens directly.
- **Confirmation:** confirms on tap (D-36), unchanged; it never had a button.

Removed with the buttons:
- `settings_action`;
- the durable `manage_request_token` that rode in every notification;
- `POST /api/v1/manage/request` and its per-subscriber rate limit;
- the action handling and `tell()` in `sw.js`;
- the `#r=` handling in `manage.html`.

That is one long-lived token and a fair amount of code fewer. The comment in `mail.py` that
argued against a second, destructive button is now moot: there are none.

**Notifications already in the tray at deploy time** still carry the old button, with a
request token valid for up to 365 days (`manage_request_ttl_days`) pointing at the removed route.
The new `sw.js` treats a click on any action like a click on the notification itself: it opens
`data.url`. One case in `tests/js/sw_test.mjs` covers it. An old service worker still running
would POST, get a 404 and fall back to its "Tippe hier" message, which opens `/manage` -
acceptable.

This part does not depend on the device key and can ship first. Without a key, a subscriber
reaches settings via the site link and the link step, about the same number of taps as the
button today.

### 4.7 Data and housekeeping

New table `device_keys`:

| column | type | note |
|---|---|---|
| `id` | text PK | `base64url(SHA-256(SPKI))`, the `key_id` |
| `subscriber_id` | FK → subscribers, `ON DELETE CASCADE`, **unique** | one key per subscriber |
| `public_key` | `LargeBinary` | SPKI DER, ~91 bytes; public, not a secret |
| `created_at`, `last_used_at` | timestamptz | `last_used_at` written at most once a day |

- **Grants** come from the default privileges set in `d5a1c7e93b42`.
- **Deletion:** unsubscribing, 410 pruning and the liveness deletion (D-46) delete the key through
  the cascade.
- **No key expiry:** this was considered and declined. A 90-day expiry would bound a key planted
  by injected script (§7), but it would send every subscriber through an unexplained link step
  each quarter, which contradicts "subscribed or not". The CSP remains the defence.
- **Liveness:** the job counts "a settings link was issued" as a sign of a living reader
  (`count_silent_subscribers`). With buttons and request tokens gone, the better signal is the
  **tap on a warning**. Every tap reaches `POST /api/v1/locate` with a signed token that names the
  subscriber, which proves the warning was displayed and opened (review 3). Recorded as a
  per-subscriber `last_seen_at`, it is joined by key use (`last_used_at`) and by links still
  issued on the fallback path. Without these, people who do read their warnings would be asked
  whether they are still there.

### 4.8 Limits and kill switch

Failed signature checks are counted per IP in a bucket of their own (about 60/h, via
`hit_and_check`). Successful requests fall under the limits those routes already have. The
link-request bucket (`manage:ip`, 5/h) is not shared (review 1, Low).

Per-IP limits are **not** a defence on their own: §12 records a pre-existing way around them
through the directly reachable `*.run.app` address. Nothing in this plan relies on them for
safety - a failed signature is refused whatever the count - so they only reduce noise.

`DEVICE_KEY_LOGIN_ENABLED` defaults to `true` and is a Terraform variable. With it off:
- no enrolment happens;
- `RainKey` requests answer a plain 401, so pages fall back to the link step;
- redemption sets the cookie.

Nobody is locked out and no data is lost. If it is ever thrown because of a verification bug,
delete all rows from `device_keys` before turning it back on (RUNBOOK).

### 4.9 The API token, for push

The confirmation page shows push subscribers a non-expiring API bearer token that reads, edits and
deletes the subscription. It is the longest-lived credential they have, and this change should not
leave it standing while arguing about long-lived credentials. For push subscribers it is no longer
shown and no longer issued. Email is unchanged.

## 5. Privacy

The key is stored on the user's device. §25 (2) Nr. 2 TDDDG allows that without consent when it is
strictly necessary for a service the user asked for - here, getting into their own settings.

The privacy page gains one neutral sentence, without naming a feature. It says that the browser
stores a key with which this site recognises it as the subscribed browser; that the key is
removed on unsubscribing or by clearing site data; and that the server keeps only its public half.
The signup page's D-47 sentence ("clearing browser data ends the subscription") already covers
the behaviour and needs no change.

## 6. Work breakdown

0. ~~Check that Firebase Hosting passes `Authorization` and the raw path to Cloud Run unchanged.~~
   Done, §11: it does; `Authorization` stays the header.
1. **Notifications without buttons (§4.6)** - independent, can ship first. Drop
   `settings_action`, `manage_request_token` and `verify_manage_request_token`,
   `/api/v1/manage/request` and its limit, the `sw.js` action handling and `tell()`, and `#r=`;
   action clicks open `data.url`.
   Known dependants (review 3):
   - `liveness.py` 103, 138, 196-216;
   - `mail.py` 15, 150-167, 354-360;
   - `config.py` 290, 307;
   - `tokens.py`;
   - `tests/test_pages.py`, `tests/test_manage.py` (~15 references), `tests/test_webpush.py`,
     `tests/js/sw_test.mjs`;
   - `RUNBOOK.md` 782, `LOCAL.md` 402, three references in DESIGN.md.

   The liveness signal moves to warning taps (§4.7).
2. Migration: `device_keys`. Model + cascade.
3. `rainalert/devicekeys.py`: parse/validate SPKI (P-256 only), build the canonical message, verify
   P1363 signatures, the time window.
4. Service: endpoint/p256dh check on every push token redemption; enrol (in `confirm` and
   `redeem_manage_token`); key deletion via cascade.
5. API: `RainKey` branch in `current_subscriber`; `device_key`, `endpoint` and `p256dh` on
   `/confirm` and `/api/v1/manage/session` (cookie only when no key was enrolled); failure
   reasons; `no-store`; limits.
6. `static/devicekey.js` (shared by `confirm.html` and `manage.html`): generate, store (or keep in
   memory), load, `signedFetch`, server-time offset, delete. Every call is wrapped so that any
   failure means "no key".
7. `manage.html`: key mode in `start()` (no countdown or extend); silent daily rotation;
   `unknown_key` reload-and-retry; the `client` version field; reload at most once; remove "Sitzung
   auf diesem Gerät beenden" for push and email alike; delete the local key after unsubscribing. Liveness
   counts warning taps and key use. API token not issued for push.
8. Setting + Terraform variable; privacy sentence.
9. Docs: DESIGN D-64; SECURITY_REVIEW entry; RUNBOOK (kill switch and purge; "settings open
   without a link" is now expected).
10. Tests:
    - **Unit:** the canonical message; good signature; wrong key; other origin, method, path or
      body; percent-encoded paths; a refused query; time window edges; non-digit `t`; P1363
      length; malformed or non-P-256 SPKI.
    - **API:**
      - a refused redemption (`push_mismatch`, `stale_page`) leaves the token usable;
      - rotation only with a valid signature from the current key;
      - enrolment only with a valid push token - **never with a token for another subscription**
        (endpoint or p256dh), never at subscribe, never for email;
      - `push_mismatch` and `stale_page`;
      - no cookie when enrolled;
      - `RainKey` ignores cookies, never falls back to them, and needs no CSRF;
      - `unknown_key` only for unknown keys; `clock`;
      - `Cache-Control: private, no-store` on every authenticated API response;
      - cascade on unsubscribe; limits; kill switch; liveness counting;
      - notifications carry no actions; `/api/v1/manage/request` is gone.
    - **Chromium end to end:**
      - confirm → reopen `/manage` → panel, with no push sent and no cookie set;
      - save a setting;
      - foreign confirm link → refused with the subscribe-again explanation;
      - unsubscribe → the local key is gone and the gate shows;
      - a clock 10 minutes off still works;
      - two tabs, one re-enrolling or rotating → the newer key survives and neither tab falls
        to the link step;
      - an old-script tab reloads once, never twice;
      - no user-visible text mentions a key, a session or quick access.

Estimated size: ~280 lines of new Python and ~150 of JavaScript, plus tests. Step 1 *removes*
more than it adds.

## 7. Residual risks, stated plainly

- **Someone with the unlocked phone** opens the settings directly - as today, one tap faster.
- **Script injected into our origin** can sign requests with the reader's key while it runs. Worse,
  it can request a settings link, read the token from `getNotifications()`, and redeem it with a
  key of *its own* making. The endpoint check does not stop this, because the script runs in the
  right browser and can read the endpoint. A key generated extractable can be sent away, and
  re-imported as non-extractable into the reader's own IndexedDB so the reader notices nothing.
  Binding the key to the push subscription would not help: the script can read and send away
  the endpoint, `p256dh` and `auth` as well. What bounds it is **silent rotation** (§4.5). The
  reader's next visit replaces a planted key with one the attacker never saw, or, if the attacker
  rotated first, sends the reader through the link step, which evicts the attacker. So the
  window is "until the reader next opens settings", not "until they unsubscribe". The nonce-only
  CSP remains the first defence. Cheap extra: close the `rainalert-manage` notifications once a
  link is redeemed.
- **A replay window** of two minutes for an identical request (§4.4).
- **Malware copying the whole browser profile** gets the key - as it gets the push keys today.
- **Safari on macOS without "Add to Dock"** may delete IndexedDB after 7 days without a visit
  (ITP). The link step re-enrols. Installed PWAs (iOS web push requires one) are exempt.
- **The `*.run.app` address answers directly** (§11). A signed request sent there is accepted:
  the server builds the origin from `public_base_url`, so a signature verifies wherever it lands.
  This is harmless - it is the same service, and a page served from run.app has no key of its
  own. (Revision 3 said such a request fails; review 3 corrected it.)

## 8. Security review 1 (2026-10-09, on revision 1)

An independent review (pragmatic brief: real attacks against this service's threat model, the
smallest fix for each) concluded **"build with changes"**:

| Severity | Finding | Status in revision 3 |
|---|---|---|
| High | Enrolment not bound to the browser holding the push subscription: an attacker's own confirm/settings link, opened by the victim, would permanently bind the victim's browser to the attacker's subscription | §4.1 - widened to every push token redemption |
| Medium | Deleting the key on any 404 wipes everyone's keys on a rollback, traffic split or kill switch | §4.2 `unknown_key` |
| Medium | The notification button POSTs from the service worker, so the main saving would miss the most common path; liveness counts issued links as activity | superseded: no buttons (§4.6); liveness §4.7 |
| Low | XSS gains more than stated | §7 |
| Low | Sign-out: keep cookie deletion unconditional; "abmelden" means unsubscribe here | moot: no sign-out control (§4.5) |
| Low | Server-chosen `key_id` cannot reach script on the form-POST confirm path | §4.1 hash id |
| Low | Non-atomic single use of the login challenge, `auth_tokens` growth | moot: no challenge |
| Low | Shared `manage:ip` bucket (5/h) would lock out key logins | §4.8 |
| Note | Session expiry texts become wrong | §4.5: no session in key mode |
| Note | Comparison with A overstated; the API token is the real long-lived credential | §3, §4.9 |
| Note | Migration downgrade order (enum); kill-switch purge | enum gone; §4.8 |

Found sound in revision 1 and carried over:
- enrolment only with a push-delivered single-use token, never at subscribe time and never for
  email;
- the domain-prefixed, server-built signed message;
- P1363 handling and P-256-only SPKI;
- key replacement cannot be triggered remotely;
- no new CSP or service-worker exposure;
- cascade deletion;
- the TDDDG basis (a sanity check, not legal advice).

## 9. Why revision 2 dropped the session

Once the browser can prove "I am the subscribed browser" at any moment for about a millisecond, a
session no longer does anything useful:

| What a session is for | With a device key |
|---|---|
| Not repeating an expensive proof | The proof is cheap; nothing to save |
| Time-limiting access on the device | Already meaningless in revision 1: after expiry the page silently signed in again |
| Time-limiting a leaked cookie | No cookie, nothing to leak; a captured request is good for itself, for two minutes |
| Revocation | Better without: deleting the key ends access on the next request, while a signed session cookie lives until it expires |
| Spending the proof on many requests | Every request carries its own |

What goes with it for key holders:
- the session cookie;
- the CSRF value and header;
- the 30/120-minute rules;
- the extend route and button;
- the expiry texts;
- revision 1's challenge route and table.

CSRF disappears as a class, because nothing is attached to requests automatically.

What stays is the cookie session for email and for browsers without WebCrypto - existing code,
kept rather than written. If email later enrols keys too (a mailed magic link registering this
browser), sessions and CSRF could be deleted entirely.

## 10. Security review 2 (2026-10-09, on revision 2)

The same reviewer and the same brief. Verdict: **build it, with changes**. Dropping the session
for key holders is the right call: once a signature is cheap, the session no longer protects
anything, and CSRF really does go away for key holders. Nothing in revision 2 is a design-level
flaw.

| Severity | Finding | Status in revision 3 |
|---|---|---|
| Medium | The endpoint check now guards every push redemption, so a browser whose endpoint string changed while pushes still arrive would be locked out of settings - and of deleting its data - with no explanation | §4.1: endpoint **or** p256dh; `push_mismatch` explained and counted |
| Low | Two tabs: deleting on `unknown_key` destroys the newer key another tab just enrolled | §4.5 compare-and-delete; §4.1 compare-and-set |
| Low | XSS can plant an extractable key re-imported as non-extractable: invisible, effectively permanent | §7, stated plainly; expiry considered and declined (§4.7) |
| Low | Canonicalisation: raw path not decoded path; hash the received body; async body read; no cookie fallback on a failed `RainKey`; digits-only `t`; the plan named a non-existent `PUT .../rule` | §4.2 |
| Low | Firebase Hosting caches by the `__session` cookie, which key-mode requests lack | §4.2 `private, no-store` on authenticated API responses |
| Note | No replay store is acceptable | §4.4 |
| Note | `performance.now()` pauses during sleep: the clock retry must cover every signed call | §4.3 |
| Open | Does Hosting forward `Authorization` and the raw path unchanged? | resolved, §11 |
| Open | Tabs still running the old script after a deploy post no endpoint | §4.1 `stale_page` → reload |

Found sound:
- No CORS on the app (the CORS block in `infra/main.tf` belongs to the overlay bucket), so
  `Authorization` forces a failing preflight cross-site, and a form cannot set it.
- Key mode sets no cookie.
- The service worker POSTs only URLs from our own payloads.
- The canonical message is unambiguous: fixed order, and no field can contain a newline.
- Keys are deleted only on `unknown_key`, and the kill switch cannot wipe them.
- The endpoint check closes today's login CSRF.
- The hash-based key id.
- No API token for push.
- Liveness via `last_used_at`.
- The tab-only key: a context without the push subscription is refused, so it can never replace
  the real key.
- No abuse path in the time sync.

Revision 3 has not had a third review. Its changes remove things - buttons, a token, a route, a
sign-out control, an expiry and two messages - rather than add mechanism. The one weakening is
spelled out in §7: no expiry and no visible trace for an XSS-planted key.

## 11. Revision 3: product decisions and the infrastructure check

**Product decisions (2026-10-09):**
- **Subscribed or not.** The user never sees a key, a session or "quick access". The only account
  controls are subscribing (start page) and unsubscribing (settings page). "Sitzung auf diesem
  Gerät beenden" is removed for everyone, and so is the planned remove-this-browser control.
  Messages about a replaced or removed key are dropped: the page falls back to the link step
  silently.
- **No buttons on notifications.** Warnings, liveness and confirmation open a page when tapped and
  offer nothing else. Settings and unsubscribing live on the settings page only.
- **No key expiry.** See §4.7 for the trade-off.

**Step 0, run against production on 2026-10-09 through `https://rainalerts.web.app`:**

| Probe | Result | Meaning |
|---|---|---|
| no `Authorization` | `401 missing bearer token` | baseline |
| `Authorization: Bearer probe` | `401 not authorised` | header forwarded |
| `Authorization: RainKey ...` | `401 not authorised` | custom scheme forwarded; no need for `X-Rain-Key` |
| `/API/v1/...` | `404` | path forwarded unchanged, not case-folded |
| `/.../me/` | `307` from the app, `Location: http://rainalert-api-pcrrzqfncq-ey.a.run.app/...` | trailing slash forwarded; **the app sees the Cloud Run host and `http`** |
| `/...//me` | `307` from Firebase Hosting | Hosting redirects double slashes rather than rewriting them silently; the page never builds such paths |
| `/.../%6De` | `401` | reaches the route; whether Hosting decodes is not observable because the app decodes either way. The settings routes contain nothing a browser encodes |
| twice the same API GET | `x-cache: MISS` both times, no `age` | the CDN is in the path but did not cache; `no-store` keeps it so |
| `https://rainalert-api-pcrrzqfncq-ey.a.run.app/api/v1/subscriptions/me` | `401 missing bearer token` | the run.app address answers directly (`/healthz` 404s there because Cloud Run reserves it) |

**Side finding, not part of this plan:** FastAPI's automatic trailing-slash redirect builds its
`Location` from the request it sees. It therefore leaks the internal `*.run.app` hostname and
downgrades to `http`. A separate small fix: turn `redirect_slashes` off, or answer from
`public_base_url`.

## 12. Security review 3 (2026-10-09, on revision 3)

The same reviewer and the same brief. Verdict: **build it, with two small changes**. Removing the
buttons, the request token and the sign-out control opens no security hole.

| Severity | Finding | Where addressed |
|---|---|---|
| Medium | `push_mismatch`/`stale_page` checked after the token is spent: a reload meets a spent token and an unconfirmed signup is purged. "No endpoint = old script" also matches a current script that cannot read its subscription, which would reload forever | §4.1: checks before spending, an explicit `client` version, reload at most once, null subscription → `push_mismatch` |
| Low | An XSS-planted key lasts forever; binding to the push subscription does not help, rotation does | §4.5 silent daily rotation, `unknown_key` reload-and-retry; §7 |
| Low | Email on a shared computer has no sign-out any more: the next person gets up to 30 min (sliding, 120 max) of access | accepted: no sign-out for push or email (§4.5) |
| Low | Notifications still in the tray carry buttons pointing at the removed route | §4.6: action clicks open `data.url`; test |
| Note | Code and docs still depending on what is removed | §6 step 1 |
| Note | Warning taps (`POST /api/v1/locate`) are a better liveness signal than issued links | §4.7 |
| Note | §7's claim that signed requests to run.app fail was wrong (harmless either way) | §7 corrected |
| Note | Compare `p256dh` as bytes | §4.1 |
| Note, **pre-existing** | With `trusted_proxy_hops = 2` (recommended behind Firebase Hosting), a request sent straight to the run.app address chooses its own client IP via a forged `X-Forwarded-For` (`api/ratelimit.py` `client_ip`), bypassing every per-IP limit in the service | §4.8 (nothing here relies on it); a separate fix, §13 |

Found sound:
- Removing the buttons and the 365-day request token removes the longest-lived push credential
  after the API token. Settings stay reachable through the site link, and deletion no longer
  depends on a push arriving.
- The endpoint-or-p256dh match stays safe against login CSRF.
- Local key deletion after unsubscribing, plus the cascade.
- Step 0's results; `no-store`; the origin from configuration; the kill switch.
- Dropping the "replaced" message, now that rotation carries the XSS case.
- The only other place that builds a URL from the request host is the trailing-slash redirect
  already found (§11); templates use only `request.url.path`.

## 13. Still open

- **Separate fix - the run.app bypass of per-IP limits** (pre-existing, not part of this plan).
  **Confirmed live:** production runs with `trusted_proxy_hops = 2`, so every per-IP limit can be
  sidestepped by calling the run.app address with a forged `X-Forwarded-For`. Options to
  evaluate:
  - make the direct address unusable for the API, if Firebase Hosting can still reach the service
    then (ingress and Hosting compatibility to verify);
  - or check a header that only Hosting's egress sets and a client cannot forge;
  - or put a load balancer in front.
- Whether an iOS home-screen web app stores a `CryptoKey` in IndexedDB reliably. This needs a real
  device. Any failure must count as "no key", which means the link step.

## 14. Implementation notes (2026-10-09)

Built as described, with these differences, each found while building or testing:

- **Pages from before the release are redeemed as before, not reloaded** (§4.1's `stale_page`).
  An old script cannot be told to reload itself - it does not know the new answer - and `/confirm`
  is a form POST with no script on the way back. So a redemption without the `client` field is
  handled exactly as it was before this change: no proof asked, no key, the cookie session. That
  path cannot be driven from another site (next point), and it never registers a key, so review 1's
  High finding stays closed. There is nothing to loop on.
- **Cross-site POSTs are refused by `Sec-Fetch-Site`, not `Origin`.** Found in Chromium: our pages
  send `Referrer-Policy: no-referrer`, which makes the browser send `Origin: null` on its own
  same-origin POSTs, so an `Origin` check refused every real confirmation. `Sec-Fetch-Site` is set by
  the browser and cannot be set by a page. Browsers that do not send it (older than 2021-2023) fall
  back to `Origin`, letting `null` through.
- **Refusal reasons arrive as `{"detail": {"error": ...}}`**, FastAPI's shape, rather than the
  bare `{"error": ...}` the text shows.
- **The liveness signal is `subscribers.last_seen_at`** (a tapped warning, a key-signed settings
  request, a redeemed settings link, at most one write a day), rather than `device_keys.last_used_at`
  alone. `device_keys.last_used_at` is still kept.

Verified in Chromium against the running app, with the push subscription stubbed (no push service
is reachable from the sandbox):
- confirming registers a non-extractable key and sets no cookie;
- the settings page opens and saves with signed requests only, also with the phone's clock ten
  minutes off;
- rotation replaces the key and the new one works;
- another subscriber's confirmation link is refused, leaves the key untouched, and still works in
  its own browser;
- unsubscribing removes the key from IndexedDB;
- a browser without a key goes through the push link once and then opens directly.


# Plan: a device key instead of push round trips and sessions

Status: **proposal, revision 2 (2026-10-09)** - not implemented. Revision 1 replaced the push
round trip with a key that opened the ordinary session; its security review is in §8. Revision 2
drops the session for key holders altogether: every request is signed (§9 says why). Its own
review (§10) found no design-level flaw; its findings are folded in. Scope: web push subscribers only; email is unchanged. Would become DESIGN.md
D-63 once agreed.

## 1. The problem

A push subscriber who opens `/manage` without a live session cookie (sessions last 30 minutes,
at most 120, D-25) gets there by a detour: the page sends its push endpoint to
`POST /api/v1/manage/link`, the server pushes a notification carrying a single-use, 15-minute
`#t=` link, and tapping it opens the session. The "Einstellungen" button on every notification
does the same from the service worker: it POSTs a durable request token to
`/api/v1/manage/request` (`sw.js`, `mail.py` `settings_action`), which pushes the link.

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
confirming the subscription or redeeming a settings link that arrived as a push - the page also
registers a **non-extractable ECDSA P-256 key pair** created by WebCrypto and kept in IndexedDB.
From then on **every request the settings page makes is signed** with that key - method, path,
body and time - and the server authenticates each one on its own. There is no session, no
cookie, no CSRF value and nothing to expire for key holders. The push link stays as the way a
key is (re)registered, and the session cookie stays only for email and for the rare browser that
cannot hold a key.

## 3. Why a device key (option B)

The alternatives considered (from the discussion on 2026-10-09):

- **A. A long-lived device cookie** (`__Host-`, HttpOnly, SameSite=Strict, ~1 year, rotated).
  Simple, and lives exactly as long as the push subscription (both are site data; D-47). But it
  is a long-lived *bearer* credential: its bytes are the key. A HAR file sent with a bug report,
  a cookie exporter, a devtools screenshot - any of them hands over standing access to someone's
  home coordinates. A would be acceptable for this threat model; B is better only against those
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
- **D. WebAuthn / passkeys.** The strongest hardware-backed option and the one to reach for if
  this ever holds anything more sensitive. Rejected for now: every use shows a system prompt
  (biometric/PIN), passkeys may sync to a Google or Apple account (no longer "this browser"),
  and the UX and code cost buys protection against an attacker - the one holding the unlocked
  phone - that the radar's threat model accepts.
- **Keep the round trip.** Safe; but see §1: it pays mostly in availability and friction.

Honest caveat: the longest-lived credential a push subscriber has today is not a session but
the API token the confirmation page shows them (`confirmed.html`); §4.10 removes it for push.

## 4. Design

### 4.1 Enrolment - the key rides in with an existing proof

No new proof is invented. A key is accepted **only together with** a push-delivered single-use
token being redeemed in the same request:

- **Confirmation** (`POST /confirm`, the `#a=` path that confirms on open, D-36): the confirm
  page generates the key pair and stores it as *pending* in IndexedDB before submitting, and adds
  hidden `device_key` (SPKI, base64url) and `endpoint` fields to the form.
- **Settings link** (`POST /api/v1/manage/session`, the `#t=` path): `redeem()` sends
  `device_key` and `endpoint` alongside `token`.

**The redeeming browser must hold the push subscription the token belongs to**: the page sends
its subscription's `endpoint` and `p256dh` key, and the server redeems a webpush subscriber's
token only if `hash_address('webpush', endpoint)` equals that subscriber's `address_hash` **or**
the `p256dh` equals the stored `push_p256dh`. Either match is equally safe against the attack
below, because a victim's browser presents the victim's values, which never match the attacker's
subscriber. Accepting either one keeps a browser that re-encodes or migrates its endpoint string
from being locked out (review 2). This applies to every push token redemption, with or without a
key.
A push-delivered token can only be opened in the browser holding that subscription, so a
legitimate redemption always passes. An attacker's own link sent to a victim always fails, which
closes review 1's High finding and also today's two-hour login CSRF.

A mismatch is not silent. It answers `{"error": "push_mismatch"}`, and the page explains that this
browser no longer holds the subscription the link was for, and offers to sign up again. Each
mismatch is logged as a counted event, so a systematic lockout shows up rather than hiding. A
request with **no** endpoint at all answers `{"error": "stale_page"}`: that is a tab still running
the previous script after a deploy (the service worker reuses open tabs, D-49), and the page
reloads itself instead of refusing. Then:

- key present and valid (uncompressed P-256 SPKI, length capped) and the feature on → the key is
  stored (replacing any previous one; one key per subscriber) and **no cookie is set**. The
  answer says `{"enrolled": "<key_id>"}`; only then does the page mark its pending key active.
- no key (WebCrypto unavailable) or feature off → today's behaviour: the session cookie is set.

`key_id` is `base64url(SHA-256(SPKI))`, computed on both sides. A server-chosen id has no clean
way back to script on the confirm path, which is a top-level form navigation. The confirmed page
renders `data-key-enrolled="<key_id>"` for the script to read. Promoting the pending key is a
compare-and-set: only if the pending entry is still the one with that `key_id` (two tabs, §4.5).

If IndexedDB is unavailable but WebCrypto works, the key is still enrolled and kept **in memory
for that tab only**. It works until the tab closes. The next visit goes through the push link,
which enrols a fresh key. One mechanism, not a second code path.

**Why not "any authenticated session or signed request may enrol a key"?** Because a credential
that leaks would then turn temporary access into permanent access. Tying enrolment to the
single-use push token keeps the key exactly as strong as the push round trip it replaces.

**Why not at subscribe time?** `POST /subscriptions` is unauthenticated. An attacker who knew a
victim's endpoint could subscribe it with *their own* key, and because a push confirmation
confirms on open (D-36), one tap by the victim would bind the attacker's key.

### 4.2 Signed requests

Every subscriber-scoped API call the settings page makes (`GET`, `PATCH` and `DELETE
/api/v1/subscriptions/me`, `PUT /api/v1/subscriptions/me/location`, `DELETE /api/v1/device-key`)
carries

    Authorization: RainKey key=<key_id>, t=<unix seconds>, sig=<base64url P1363 signature>

over this message (UTF-8, `\n`-separated):

    rainalert-request-v1
    <origin from public_base_url, normalised to scheme://host[:port]>
    <METHOD>
    <path, exactly as received: scope["raw_path"], not the percent-decoded url.path>
    <t>
    <base64url(SHA-256(the body bytes actually received))>

No settings call has a query string, so a `RainKey` request that carries one is refused rather
than canonicalised. `t` must be digits only.

`current_subscriber` gains a third branch beside `Bearer` and the cookie. For a `RainKey` header it
looks up the key, checks that `t` is within ±120 s of the server clock, verifies the signature
over the message it builds from **its own** origin and the request it actually received, and
returns the key's subscriber. Reading the body needs `await request.body()` while
`current_subscriber` is synchronous, so the hashing sits in a small async dependency and the
database work stays synchronous. A request with this header is authenticated by it alone. If the
check fails, the answer is 401: there is **no fallback** to a cookie on the same request, which
is ignored, and no CSRF value is required. That's D-27's rule for header
credentials, which nothing cross-site can attach: our API sends no CORS headers, so another site
cannot set `Authorization` on a request to us, and it could not sign one anyway.

Every authenticated API response carries `Cache-Control: private, no-store`. Firebase Hosting's
cache key is the `__session` cookie, and key-mode requests carry none: if an authenticated GET
were ever cacheable, every key holder would share one cache entry, which means someone else's
home coordinates (review 2). This holds for API responses only; HTML pages keep their
bfcache-friendly `private, no-cache`.

Whether Firebase Hosting forwards `Authorization` and the raw path to Cloud Run unchanged is
checked end to end before anything else (§6, step 0). If it interferes, the header becomes
`X-Rain-Key`, which needs the same failing preflight cross-site and so keeps the same CSRF
property.

Signatures are 64 bytes raw `r||s` (IEEE P1363) from WebCrypto. The server rejects any other
length and converts with `encode_dss_signature`. Malleability is irrelevant: a mutated signature
authorises the same request. Verification is ~0.2 ms; the browser signs in ~1 ms.

Failures answer 401 with a reason the page acts on: `{"error": "unknown_key"}` (no such key - the
page deletes its key, §4.5), `{"error": "clock"}` (outside the window - the page resyncs and
retries once, §4.3), anything else plain 401 (the page keeps its key and offers the push link).
**The page deletes its key only on `unknown_key`.** A rollback, a traffic split or the kill
switch answer without that body and cannot wipe everyone's keys (review 1, Medium).

### 4.3 Time

Phone clocks drift, so the page does not trust its own. It signs with server time: the page
renders the server's clock (`data-server-time`), and the page keeps an offset to
`performance.now()`, refreshed from the `Date` header of every response. Same-origin fetch can
read `Date`. On `{"error": "clock"}` the page takes the offset from that response and retries
once - **every** signed call, the two DELETEs included. `performance.now()` pauses while iOS
and macOS devices sleep, so the first request after waking will often get `clock`. A page
restored from the back/forward cache with an old render time heals the same way.

### 4.4 Replay

There is no per-request nonce store, deliberately. A captured signed request can be replayed only
**unchanged**: same method, path and body, within two minutes. To capture one, an attacker
has to break TLS, run script in our origin (which can sign for itself anyway), or be handed a HAR
file within two minutes of its recording. Replaying a GET shows the subscriber's data once.
Replaying a PUT sets the same values again. Replaying a DELETE deletes what was already deleted.
A nonce table would cost a database write per request to close a window this narrow. If review
disagrees, the cheap version is a nonce store for non-GET requests only. Review 2 agreed with
leaving it out. The worst case is an old PATCH replayed after a newer one, which reverts a
setting - only for someone who already captured the request.

### 4.5 The page

`/manage` without a fragment: load the key from IndexedDB → signed `GET /api/v1/subscriptions/me`.

- 200 → the panel. **No session countdown and no "Verlängern" button** in key mode, because there is
  nothing to expire.
- `unknown_key` → delete the key **only if the stored `key_id` is still the one that was
  refused** (compare-and-delete). Then say "Der Schnellzugang auf diesem Gerät wurde ersetzt oder
  entfernt." and show the gate. Without the compare, a tab still on the old key would delete the
  newer key another tab had just enrolled, and show a false misuse warning (review 2).
- no key / other failure → the gate, i.e. the push link as today, which re-enrols.

`#t=` links redeem and enrol as in §4.1, then continue as above. The session-expiry texts
("Sitzung ist abgelaufen - fordere einen neuen Link an") remain only for cookie mode.

A small `signedFetch()` wrapper in `static/devicekey.js` replaces `fetch` for the authenticated
calls. In cookie mode it is plain `fetch` with the CSRF header, as now.

### 4.6 Fallbacks and the existing paths

- No WebCrypto, feature off → cookie session, unchanged.
- **The notification's "Einstellungen" button opens `/manage`** instead of POSTing a request
  token from the service worker. The page uses the key, or falls back to the gate. This is where
  most of the saving is (review 1, Medium): the button is the most common way into settings, and
  today every tap costs a push. No token rides in the URL. The durable request token then has no
  remaining user and goes, with the `#r=` handling in `manage.html`, which nothing emits any more.
- Email subscribers: untouched - magic link, cookie, CSRF.

### 4.7 Removing quick access on this device

"Sitzung auf diesem Gerät beenden" becomes **"Schnellzugang auf diesem Gerät entfernen"**: not
"abmelden", which on this site means unsubscribing. The page deletes its IndexedDB key first,
whatever happens next, then sends a signed `DELETE /api/v1/device-key`. Being signed, it needs no
CSRF, and another site cannot send it. It also clears any cookie unconditionally, as today. The
next visit goes through the push link, which re-enrols.

### 4.8 Data and housekeeping

New table `device_keys`:

| column | type | note |
|---|---|---|
| `id` | text PK | `base64url(SHA-256(SPKI))`, the `key_id` |
| `subscriber_id` | FK → subscribers, `ON DELETE CASCADE`, **unique** | one key per subscriber |
| `public_key` | `LargeBinary` | SPKI DER, ~91 bytes; public, not a secret |
| `created_at`, `last_used_at` | timestamptz | `last_used_at` written at most once a day |

Keys expire **90 days after enrolment**: a `RainKey` request with an older key answers
`unknown_key`, and the page re-enrols through the push link. That costs a reader one link about
every three months. In exchange, a key planted by injected script (§7) cannot last indefinitely.

No enum change and no challenge rows: revision 1's login challenge is gone. Grants come from the
default privileges set in `d5a1c7e93b42`. Unsubscribing, 410 pruning and the liveness deletion
(D-46) delete the key through the cascade. The liveness job counts "a settings link was issued"
as a sign of a living reader (`count_silent_subscribers`). A signed request within the window
must count too, through `last_used_at`, or people who use the key would be asked whether they
are still there.

### 4.9 Limits and kill switch

Failed signature checks are counted per IP in a bucket of their own (about 60/h, via
`hit_and_check`). Successful requests are governed by the limits those routes already have. The
link-request bucket (`manage:ip`, 5/h) is not shared (review 1, Low).

`DEVICE_KEY_LOGIN_ENABLED` (default `true`, Terraform variable). Off: no enrolment; `RainKey`
requests answer plain 401, so pages fall back to the push link, and redemption sets the cookie.
Nobody is locked out and no data is lost. If it is ever thrown because of a verification bug,
delete all rows from `device_keys` before turning it back on (RUNBOOK).

### 4.10 The API token, for push

The confirmation page shows push subscribers a non-expiring API bearer token that reads, edits
and deletes the subscription. It is the longest-lived credential they have, and this change
should not leave it standing while arguing about long-lived credentials. For push subscribers it
is no longer shown (and not issued); email is unchanged.

## 5. Privacy

The key is stored on the user's device. §25 (2) Nr. 2 TDDDG allows that without consent when it
is strictly necessary for a service the user asked for - here, getting into their own settings.
The privacy page gains one sentence: what is stored (a key that only this site can use to prove
it is the same browser), why, and that "Schnellzugang auf diesem Gerät entfernen" or clearing
site data removes it. The signup page's D-47 sentence mentions it too. The server stores only
the public key.

## 6. Work breakdown

0. Check first, against the deployed stack: does Firebase Hosting pass `Authorization` and the
   raw path to Cloud Run unchanged? It decides the header name (§4.2).
1. Migration: `device_keys`. Model + cascade.
2. `rainalert/devicekeys.py`: parse/validate SPKI (P-256 only), build the canonical message,
   verify P1363 signatures, the time window.
3. Service: endpoint check on every push token redemption; enrol (in `confirm` and
   `redeem_manage_token`); forget.
4. API: `RainKey` branch in `current_subscriber`; `device_key`/`endpoint` on `/confirm` and
   `/api/v1/manage/session` (cookie only when no key was enrolled); `DELETE /api/v1/device-key`;
   failure reasons; limits.
5. `static/devicekey.js` (shared by `confirm.html` and `manage.html`): generate, store (or keep in
   memory), load, `signedFetch`, server-time offset, forget. Every call is wrapped so that any
   failure means "no key".
6. `manage.html`: key mode in `start()` (no countdown/extend), drop `#r=`, the "Schnellzugang"
   control. Notification button opens `/manage` (`mail.py`, `sw.js`). Liveness counts key use.
   API token not issued for push.
7. Setting + Terraform variable; privacy and signup text.
8. Docs: DESIGN D-63, SECURITY_REVIEW entry, RUNBOOK (kill switch and purge; "settings open
   without a link" is now expected).
9. Tests:
   - **Unit:** the canonical message; good signature; wrong key; other origin, method, path
     or body; percent-encoded paths; refused query; time window edges; non-digit `t`; P1363
     length; malformed or non-P-256 SPKI.
   - **API:** enrolment only with a valid push token, **never with a token for another
     subscription** (endpoint or p256dh), never at subscribe, never for email; `push_mismatch`
     and `stale_page`; no cookie when enrolled; `RainKey` ignores cookies, never falls back to
     them and needs no CSRF; `unknown_key` for unknown and for expired keys; `clock`;
     `Cache-Control: private, no-store` on every authenticated API response; cascade on
     unsubscribe; limits; kill switch; liveness counting.
   - **Chromium end to end:** confirm → reopen `/manage` → panel with no push sent and no cookie
     set; save a setting; foreign confirm link → refused with an explanation; remove quick
     access → gate; a clock 10 minutes off → still works; two tabs, one re-enrolling → the newer
     key survives.

Estimated size: ~280 lines of Python, ~150 of JavaScript, plus tests - a little less server code
than revision 1 (no challenge route, no enum, no session issuance for keys).

## 7. Residual risks, stated plainly

- **Someone with the unlocked phone** opens the settings directly - as today, one tap faster.
- **Script injected into our origin** can sign requests with the reader's key while it runs.
  Worse, it can request a settings link, read the token from `getNotifications()`, and redeem it
  with a key of *its own* making. The endpoint check does not stop this: the script runs in the
  right browser and can read the endpoint. If that key is generated extractable, the private
  half can be sent away. It can even be re-imported as non-extractable into the reader's own
  IndexedDB, so the reader's page keeps working and **no** "ersetzt" warning ever appears. That
  is standing access from anywhere, bounded only by the 90-day key expiry (§4.8). The nonce-only
  CSP is the defence, unchanged, and makes this unlikely. Cheap extra: close the
  `rainalert-manage` notifications once a link is redeemed.
- **A replay window** of two minutes for an identical request (§4.4).
- **Malware copying the whole browser profile** gets the key - as it gets the push keys today.
- **Safari on macOS without "Add to Dock"** may delete IndexedDB after 7 days without a visit
  (ITP); the push link re-enrols. Installed PWAs (iOS web push requires one) are exempt.
- **Opened on another hostname** (the `*.run.app` address Cloud Run gives every service, if it is
  reachable): the signed origin differs from `public_base_url`, every request fails and the page
  falls back. Harmless, but worth knowing when testing.

## 8. Security review 1 (2026-10-09, on revision 1)

An independent review (pragmatic brief: real attacks against this service's threat model,
smallest fix each) concluded **"build with changes"**:

| Severity | Finding | Status in revision 2 |
|---|---|---|
| High | Enrolment not bound to the browser holding the push subscription: an attacker's own confirm/settings link, opened by the victim, would permanently bind the victim's browser to the attacker's subscription | §4.1 - and widened to every push token redemption |
| Medium | Deleting the key on any 404 wipes everyone's keys on a rollback, traffic split or kill switch | §4.2 `unknown_key` |
| Medium | The notification button POSTs from the service worker, so the main saving would miss the most common path; liveness counts issued links as activity | §4.6, §4.8 |
| Low | XSS gains more than stated | §7 |
| Low | Logout: keep cookie deletion unconditional; "abmelden" means unsubscribe here | §4.7 |
| Low | Server-chosen `key_id` cannot reach script on the form-POST confirm path | §4.1 hash id |
| Low | Non-atomic single use of the login challenge, `auth_tokens` growth | moot - no challenge |
| Low | Shared `manage:ip` bucket (5/h) would lock out key logins | §4.9 |
| Note | Session expiry texts become wrong | §4.5 - no session in key mode |
| Note | Comparison with A overstated; the API token is the real long-lived credential | §3, §4.10 |
| Note | Migration downgrade order (enum); kill-switch purge | enum gone; §4.9 |

Found sound in revision 1 and carried over: enrolment only with a push-delivered single-use
token, never at subscribe time and never for email; the domain-prefixed, server-built signed
message; P1363 handling; P-256-only SPKI; key replacement cannot be triggered remotely; no new
CSP or service-worker exposure; cascade deletion; the TDDDG basis (a sanity check, not legal
advice).

## 9. Why revision 2 drops the session

Once the browser can prove "I am the subscribed browser" at any moment for about a millisecond,
a session no longer does anything useful:

| What a session is for | With a device key |
|---|---|
| Not repeating an expensive proof | The proof is cheap; nothing to save |
| Time-limiting access on the device | Already meaningless in revision 1: after expiry the page silently signed in again |
| Time-limiting a leaked cookie | No cookie, nothing to leak; a captured request is good for itself, for two minutes |
| Revocation | Better without: deleting the key ends access on the next request; a signed session cookie lives until it expires |
| Spending the proof on many requests | Every request carries its own |

What goes with it for key holders: the session cookie, the CSRF value and header, the 30/120
minute rules, the extend route and button, the expiry texts, and revision 1's challenge route
and table. CSRF disappears as a class, because nothing is attached to requests automatically.

What stays: the cookie session for email and for browsers without WebCrypto. That is existing
code, kept rather than written. If email later enrols keys too (a mailed magic link registering
"this device"), sessions and CSRF could be deleted entirely.

## 10. Security review 2 (2026-10-09, on revision 2)

The same reviewer, same brief. Verdict: **build it, with changes**. Dropping the session for key
holders is the right call: once a signature is cheap, the session no longer protects anything,
and CSRF really does go away for key holders. Nothing in revision 2 is a design-level flaw.

| Severity | Finding | Where addressed |
|---|---|---|
| Medium | The endpoint check now guards every push redemption, so a browser whose endpoint string changed while pushes still arrive would be locked out of settings - and of deleting its data - with no explanation | §4.1: endpoint **or** p256dh; `push_mismatch` explained and counted |
| Low | Two tabs: deleting on `unknown_key` destroys the newer key another tab just enrolled, with a false misuse warning | §4.5 compare-and-delete; §4.1 compare-and-set |
| Low | XSS can plant an extractable key re-imported as non-extractable: invisible, effectively permanent | §7 reworded; §4.8 90-day key expiry |
| Low | Canonicalisation: raw path not decoded path, hash the received body, async body read, no cookie fallback on a failed `RainKey`, digits-only `t`; the plan named a non-existent `PUT .../rule` | §4.2 |
| Low | Firebase Hosting caches by the `__session` cookie, which key-mode requests lack | §4.2 `private, no-store` on authenticated API responses |
| Note | No replay store is acceptable | §4.4 |
| Note | `performance.now()` pauses during sleep: the clock retry must cover every signed call | §4.3 |
| Open | Does Hosting forward `Authorization` and the raw path unchanged? | §6 step 0; fallback header `X-Rain-Key` |
| Open | Tabs still running the old script after a deploy post no endpoint | §4.1 `stale_page` → reload |

Found sound: no CORS on the app (the CORS block in `infra/main.tf` belongs to the overlay
bucket), so `Authorization` forces a failing preflight cross-site and a form cannot set it; key
mode sets no cookie; the service worker POSTs only URLs from our own payloads; the canonical
message is unambiguous (fixed order, no field can contain a newline); deleting only on
`unknown_key`; the kill switch cannot wipe keys; the endpoint check closes today's login CSRF;
the hash-based key id; local-first key removal; no API token for push; liveness via
`last_used_at`; the tab-only key (a context without the push subscription is refused, so it can
never replace the real key); no abuse path in the time sync.

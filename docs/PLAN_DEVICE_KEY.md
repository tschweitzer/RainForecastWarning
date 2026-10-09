# Plan: open the settings page with a device key instead of a push round trip

Status: **proposal, 2026-10-09, revised after security review (§8)** - not implemented. Scope: web push subscribers only; email is
unchanged. Would become DESIGN.md D-63 once agreed.

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
From then on, opening `/manage` without a session signs a one-time server challenge with that
key and receives the ordinary session cookie. No notification, no wait. The push round trip
stays as the fallback whenever there is no usable key.

## 3. Why option B

The alternatives considered (from the discussion on 2026-10-09):

- **A. A long-lived device cookie** (`__Host-`, HttpOnly, SameSite=Strict, ~1 year, rotated).
  Simple, and lives exactly as long as the push subscription (both are site data; D-47). But it
  is a long-lived *bearer* credential: its bytes are the key. A HAR file sent with a bug report,
  a cookie-stealing extension or infostealer, a devtools screenshot, a backup of the profile -
  any of them hands over standing access to someone's home coordinates. A would be acceptable
  for this threat model; B is better only against those copy-the-bytes leaks (review, §8).
- **B. A non-extractable WebCrypto key** (this plan). Same lifetime as A, same "it is this
  browser" proof, but the private key cannot be read out by script, HAR files or cookie
  exporters - it can only be *used*, by our own origin, while the browser runs. What travels
  over the wire is a signature over a single-use, 60-second challenge: worthless after one use.
  Not better than A against malware: Chrome keeps IndexedDB key material in the profile without
  the app-bound encryption Windows cookies get, and a malicious extension can *use* the key as
  it can use a cookie. Whoever copies the profile owns the device anyway.
- **C. The push subscription's `auth` secret as a password.** It is readable by any script on
  the origin, it is stored in plaintext in our database because encryption needs it, and it
  would become a permanent password nobody can rotate without resubscribing. Rejected.
- **D. WebAuthn / passkeys.** The strongest hardware-backed option and the one to reach for if
  this ever holds anything more sensitive. Rejected for now: every login shows a system prompt
  (biometric/PIN), passkeys may sync to a Google or Apple account (no longer "this browser"),
  and the UX and code cost buys protection against an attacker - the one holding the unlocked
  phone - that the radar's threat model accepts.
- **Keep the round trip.** Safe; but see §1: it pays mostly in availability and friction.

B keeps the round trip's real security property (proof of possession of this browser, nothing
replayable on the wire) and removes its availability and friction costs, for about the same
code as A. Its residual risks are stated in §7. Honest caveat: the longest-lived credential a
push subscriber has today is not a session but the API token the confirmation page shows them
(`confirmed.html`); §4.8 removes it for push.

## 4. Design

### 4.1 Enrolment - the key rides in with an existing proof

No new proof is invented. A key is accepted **only together with** a push-delivered single-use
token being redeemed in the same request:

- **Confirmation** (`POST /confirm`, the `#a=` path that confirms on open, D-36): the confirm
  page generates the key pair before submitting and adds a hidden `device_key` field (SPKI,
  base64url) to the form.
- **Settings link** (`POST /api/v1/manage/session`, the `#t=` path): `redeem()` sends
  `device_key` alongside `token`.

Both also send the browser's own push endpoint (`pushManager.getSubscription()`). The server
stores the key only when the token verifies, the subscriber is a confirmed **webpush**
subscriber, **the endpoint hashes to that subscriber's `address_hash`**, and the key parses as
an uncompressed P-256 SPKI (length capped). If anything does not match, the token is redeemed
exactly as today and no key is stored. The page writes IndexedDB only after the server says the
key was enrolled.

**Why the endpoint check** (review, High): without it, an attacker could send the victim *their
own* confirmation or settings link. The victim's page would redeem it, enrol the victim's key
under the attacker's subscription and overwrite the victim's own key - and from then on the
victim's settings page would silently open the attacker's subscription: a pin moved "home"
lands in the attacker's account, "delete my data" deletes the wrong data. Today the same trick
buys a two-hour session; with keys it would be permanent. Checking that the redeeming browser
holds the push subscription the token belongs to closes it: an attacker's link opened in the
victim's browser finds the victim's endpoint, not the attacker's.

`key_id` is `base64url(SHA-256(SPKI))`, computed on both sides. A server-chosen id has no clean
way back to script on the confirm path, which is a top-level form navigation: the page stores the
key pending before `form.submit()` and the confirmed page marks it enrolled.

**Why not "any authenticated session may enrol a key"?** Because a session cookie that leaks
would then convert two hours of access into permanent access. Tying enrolment to the single-use
token keeps the key exactly as strong as the push round trip it replaces.

**Why not at subscribe time?** `POST /subscriptions` is unauthenticated. An attacker who knew a
victim's endpoint could subscribe it with *their own* key; the victim's browser would then get
the confirmation push, and because a push confirmation confirms on open (D-36), one tap would
bind the attacker's key to the victim's subscription. Enrolling at the moment the token is
redeemed - in the browser that received it - closes this.

One key per subscriber (a push subscriber is one browser): enrolling replaces the previous key.
The replaced key simply stops working; the page holding it falls back (§4.4).

### 4.2 Login

1. `/manage` loads with no fragment (or with `#r=`, see 4.4) and no session.
2. The page reads `{key_id, privateKey}` from IndexedDB (`rainalert` db, `device` store).
3. `POST /api/v1/manage/challenge {key_id}` → `{challenge}`. The server creates an
   `AuthToken` with a new purpose `device_challenge`, bound to the key's subscriber, stored as a
   hash, expiring in 60 s. Unknown `key_id` → 404 with body `{"error": "unknown_key"}` (a
   hash-sized id is not an oracle worth hiding). **The page deletes its key only on that exact
   answer** - any other failure keeps it, so a rollback, a traffic split or the kill switch
   (all of which also answer 404, without the body) cannot wipe everyone's keys.
4. The page signs `"rainalert-manage-login-v1\n" + location.origin + "\n" + challenge` with
   ECDSA P-256/SHA-256.
5. `POST /api/v1/manage/session/device {key_id, challenge, signature}`. The server checks:
   challenge exists, unexpired, unused, belongs to that key's subscriber; the signature verifies
   against the stored public key over the message built with **its own** `public_base_url`
   origin, normalised to `scheme://host[:port]` (a trailing slash would fail every login
   silently); the subscriber is still confirmed. The challenge is consumed atomically -
   `DELETE ... WHERE token_hash = ? AND expires_at > now() RETURNING subscriber_id` - so it is
   single use without a race and leaves no row behind. Then it sets
   `last_used_at` on the key, and calls the existing `set_session_cookie` - same 30 min sliding
   session, same 120 min wall, same CSRF value (D-25, D-27).
6. On any failure the page falls back to today's gate without an error message (a missing key is
   normal) - except `unknown_key`, which says so: "Der Schnellzugang auf diesem Gerät wurde
   ersetzt oder entfernt." A key replaced without the reader's doing is the visible trace of
   misuse (§7).

WebCrypto returns ECDSA signatures as raw `r||s` (64 bytes, IEEE P1363); `cryptography` expects
DER, so the server rejects anything that is not exactly 64 bytes and converts with
`encode_dss_signature`. Signature malleability does not matter for single-use challenges. Verification lives in a small
`rainalert/devicekeys.py` using the already-present `cryptography` dependency.

### 4.3 Data

New table `device_keys`:

| column | type | note |
|---|---|---|
| `id` | UUID PK | the `key_id` the browser holds |
| `subscriber_id` | FK → subscribers, `ON DELETE CASCADE`, **unique** | one key per subscriber |
| `public_key` | `LargeBinary` | SPKI DER, ~91 bytes; public, not a secret |
| `created_at`, `last_used_at` | timestamptz | |

Plus `ALTER TYPE token_purpose ADD VALUE 'device_challenge'` (as `c4f80ab21d63` did for
`manage`; fine inside the upgrade transaction on Postgres 16 as long as nothing in the same run
uses the value). The downgrade drops `device_keys` and deletes `device_challenge` rows before
rebuilding the enum, as `c4f80ab21d63` does. The liveness job (D-46) counts "a settings link
was issued" as a sign of a living reader (`count_silent_subscribers`); key logins must count too,
through `device_keys.last_used_at`, or people who use the key would be asked whether they are
still there. Grants come from the default privileges set in `d5a1c7e93b42`. Unsubscribing, 410
pruning and the liveness deletion (D-46) delete the key through the cascade.

### 4.4 Fallbacks and the existing paths

- No key, WebCrypto or IndexedDB unavailable (private windows, old browsers), key rejected,
  network error → today's gate and push link, unchanged.
- **The notification's "Einstellungen" button opens `/manage`** instead of POSTing a request
  token from the service worker. The page tries the key first and falls back to the gate. This is
  where most of the saving is: the button is the most common way into settings, and today every
  tap costs a push. No token rides in the URL. The durable request token then has no remaining
  user and goes, with the `#r=` handling in `manage.html`, which nothing emits any more.
- `#t=` links keep working and (re)enrol the key.
- Email subscribers: untouched in this change.

### 4.5 Ending it on this device

"Sitzung auf diesem Gerät beenden" would otherwise be meaningless - the next visit would log in
again silently. It becomes **"Schnellzugang auf diesem Gerät entfernen"** - not "abmelden",
which on this site means unsubscribing. The page deletes its IndexedDB key first, whatever
happens next; then asks the server to delete the key (session + CSRF, so another site cannot
wipe keys); the cookie is cleared unconditionally, as today, so an expired session cannot make
the button fail. The next visit goes through the push link again, which re-enrols.

Session expiry stops meaning anything on the device itself - the next load signs in again - so
the "Sitzung ist abgelaufen - fordere einen neuen Link an" texts change to a silent re-login with
the link as fallback. The short session still limits a leaked cookie.

### 4.6 Limits

Per IP and per key, through the existing `hit_and_check`, in **buckets of their own** (about
30-60/h): reusing `manage:ip` (5/h) would block key logins and link requests alike after three
settings visits, and on a carrier IP shared by many phones. A wrong signature is answered 401
like an expired challenge.

### 4.7 Kill switch

`DEVICE_KEY_LOGIN_ENABLED` (default `true`, Terraform variable). Off: the page never offers or
uses a key and the routes answer 404; everyone is back on the push round trip, no data lost. If
it is ever thrown because of a verification bug, delete all rows from `device_keys` before
turning it back on (RUNBOOK).

### 4.8 The API token, for push

The confirmation page shows push subscribers a non-expiring API bearer token that reads, edits
and deletes the subscription - the longest-lived credential they have, and one this change
should not leave standing while arguing about long-lived credentials. For push subscribers it is
no longer shown (and not issued); email is unchanged.

## 5. Privacy

The key is stored on the user's device; §25 (2) Nr. 2 TDDDG allows that without consent when it
is strictly necessary for a service the user asked for - here, getting into their own settings.
The privacy page gains one sentence: what is stored (a key that only this site can use to prove
it is the same browser), why, and that "Schnellzugang auf diesem Gerät entfernen" or clearing
site data removes it; the signup page's D-47 sentence mentions it too. The server stores only the
public key.

## 6. Work breakdown

1. Migration: `device_keys`, enum value. Model + cascade.
2. `rainalert/devicekeys.py`: parse/validate SPKI (P-256 only), build the signed message,
   verify P1363 signatures.
3. Service functions: enrol (inside `confirm` and `redeem_manage_token`), issue challenge,
   redeem challenge, forget.
4. Routes: `/api/v1/manage/challenge`, `/api/v1/manage/session/device`; `device_key` on
   `/confirm` and `/api/v1/manage/session`; logout behind session + CSRF.
5. `static/devicekey.js` (shared by `confirm.html` and `manage.html`): generate, store, load,
   sign, forget - every call wrapped so that any failure means "no key".
6. `manage.html` `start()`: key login before the gate; drop `#r=`; logout wording and
   behaviour; session-expiry texts. Notification button opens `/manage` (`mail.py`, `sw.js`);
   liveness counts key logins; API token not issued for push.
7. Setting + Terraform variable; privacy text.
8. Docs: DESIGN D-63, SECURITY_REVIEW entry, RUNBOOK (kill switch, "settings page opens without
   a link" is now expected).
9. Tests: verification unit tests (good signature, wrong key, other origin, P1363 → DER, malformed
   SPKI, non-P-256 curve); API tests (enrol only with a valid token, never via subscribe, never
   for email, **never with another subscriber's token** (the endpoint check), challenge single
   use and 60 s, `unknown_key` only for unknown keys, cascade on unsubscribe, rate limits, kill
   switch, liveness counting); Chromium end to end (confirm → reopen `/manage` → panel with no push
   sent; foreign confirm link → no enrolment; remove quick access → gate).

Estimated size: ~300 lines of Python, ~120 of JavaScript, plus tests.

## 7. Residual risks, stated plainly

- **Someone with the unlocked phone** opens the settings directly - as today, one tap faster.
- **Script injected into our origin** can do more than use the key while the page is open: it
  can request a settings link, read the token from `getNotifications()`, and redeem it with a
  key of *its own* making - standing access from anywhere until the reader's next link redemption
  replaces that key, and the reader's own page then says "Schnellzugang ersetzt". The nonce-only
  CSP makes this unlikely; it is the defence, unchanged. Cheap extra: close the
  `rainalert-manage` notifications once a link is redeemed.
- **Malware copying the whole browser profile** gets the key - as it gets the push keys today.
- **Safari on macOS without "Add to Dock"** may delete IndexedDB after 7 days without a visit
  (ITP); the fallback covers it. Installed PWAs (iOS web push requires one) are exempt.
- **Opened on another hostname** (the `*.run.app` address Cloud Run gives every service, if it is
  reachable): `location.origin` differs from `public_base_url`, every login fails and the page
  falls back. Harmless, but worth knowing when testing.

## 8. Security review (2026-10-09)

An independent review of this plan (pragmatic brief: real attacks against this service's threat
model, smallest fix each) concluded **"build with changes"**. All findings are folded in above:

| Severity | Finding | Where addressed |
|---|---|---|
| High | Enrolment not bound to the browser holding the push subscription: an attacker's own confirm/settings link, opened by the victim, would permanently bind the victim's browser to the attacker's subscription | §4.1 endpoint check |
| Medium | Deleting the key on any 404 wipes everyone's keys on a rollback, traffic split or kill switch | §4.2 `unknown_key` |
| Medium | The notification button POSTs from the service worker, so the main saving would miss the most common path; liveness counts issued links as activity | §4.4, §4.3 |
| Low | XSS gains more than §7 said | §7 |
| Low | Logout: keep cookie deletion unconditional; "abmelden" means unsubscribe here | §4.5 |
| Low | Server-chosen `key_id` cannot reach script on the form-POST confirm path | §4.1 hash id |
| Low | Non-atomic single use, `auth_tokens` growth | §4.2 `DELETE ... RETURNING` |
| Low | Shared `manage:ip` bucket (5/h) would lock out key logins | §4.6 |
| Note | Session expiry texts become wrong | §4.5 |
| Note | Comparison with A overstated; the API token is the real long-lived credential | §3, §4.8 |
| Note | Migration downgrade order; kill-switch purge | §4.3, §4.7 |

Checked and found sound: enrolment only with a push-delivered single-use token, never at subscribe
time and never for email; challenge randomness, lifetime and binding; the domain-prefixed,
server-built signed message; P1363 handling; P-256-only SPKI; key replacement cannot be triggered
remotely; CSRF on key deletion; no new CSP or service-worker exposure; cascade deletion; the
TDDDG basis (a sanity check, not legal advice).

**Open, to settle during implementation:**
- Whether an iOS home-screen web app stores a `CryptoKey` in IndexedDB reliably - needs a real
  device. Any failure must count as "no key".
- Whether production is reachable on the `*.run.app` hostname as well as `public_base_url`.

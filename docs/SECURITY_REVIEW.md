# RainAlert — Independent Security Review

**Reviewer:** external, adversarial ("hacker hat") review of `docs/DESIGN.md` (Draft v2, 2026-09-16),
`docs/DWD_RV_FORMAT.md`, the M1 code in `rainalert/`, and `tests/`.
**Date:** 2026-09-16
**Scope:** the design is the primary target; M1 code (`decoder.py`, `grid.py`, `cli.py`) is reviewed as
built. No server, DB or API exists yet, which is why this is worth doing now.

---

## Executive summary

The design is unusually careful for a hobby project. Data-handling correctness (no-data sentinel,
per-frame missing gate, georeferencing pinned against an oracle, header-driven parsing) is better
than most production weather code, the API is `/me`-shaped so there is no IDOR surface to speak of,
and the privacy story is thought about rather than bolted on. Nothing here is a rewrite.

The weaknesses cluster in three places, and all three are *availability and blast-radius* problems
rather than classic confidentiality bugs:

1. **The service trusts one unauthenticated byte-stream from the internet (`opendata.dwd.de`) with
   total control over the ingest job's memory and over every alert decision**, and has no cap on the
   damage a single bad archive can do. A 483-byte file I crafted already kills the existing decoder
   (measured below). There is no plausibility gate on the decoded field, no bound on how many mails
   one cycle may send, and no sanity check on the header-supplied nominal time — which is also the
   input to the staleness SLI that is supposed to catch exactly this.
2. **A single bad row in `subscriptions` takes the whole 5-minute pipeline down for everyone.** The
   design never states per-subscription error isolation, and the M1 grid code raises `OverflowError`
   / `ValueError` (not the documented `OutsideGrid`) for inputs a mobile app can legitimately send —
   a friend on holiday in Italy is enough.
3. **The long-lived API bearer token is issued and displayed by a `GET` link sent in email.** Email
   scanners, link prefetchers, the `Referer` sent to the OSM tile servers on the same page, browser
   history and Cloud Run's own request logs all see it. The `GET`-triggered unsubscribe is worse
   than usual because unsubscribe is a *hard delete*.

Second-order but real for a pay-per-use deployment: the design has no stated `max-instances`, an
unauthenticated `/readyz` that opens a database connection, a "public-read or proxied" GCS bucket
that also holds the raw DWD archives, and a rate-limit design that does not say where the client IP
comes from. Each of those turns an idle Sunday into a bill or into starved alerting.

Two places where the design is **silent in a way an implementer will fill in badly by default** are
flagged explicitly: the client-IP source for rate limiting (§10) and what "`/metrics` — internal"
means on Cloud Run, where per-path IAM does not exist (§10, §15).

Finally, one legal point that is mis-scoped rather than missing: §13 defers the mail-provider DPA to
"a public launch". Art. 28 GDPR applies the moment the first friend's address is handed to Brevo.

**Counts:** 6 high, 7 medium, 3 low, 2 informational. No critical: nothing here is unauthenticated
account takeover or mass data exfiltration.

---

## Findings table

| # | Title | Severity | Fix by |
|---|---|---|---|
| F-1 | bz2/tar archive from DWD is a decompression bomb with no caps (measured: 483 B → OOM) | High | Before friends (M2) |
| F-2 | No plausibility gate on decoded radar data, and no global cap on alerts per cycle | High | Before friends (M4) |
| F-3 | One bad subscription row kills the whole cycle for every subscriber | High | Before friends (M3/M4) |
| F-4 | Long-lived API token is issued and rendered by an emailed `GET` link | High | Before friends (M3) |
| F-5 | Rate limiting: client-IP source unspecified → trivially bypassed; deletion erases the abuse state | High | Before friends (M3) |
| F-6 | Scale-to-zero cost/DoS: no instance cap, unauthenticated `/readyz` opens DB connections | High | Before friends (M6) |
| F-7 | `nominal_time` is taken from attacker-controllable header and drives the staleness SLI | Medium | Before friends (M2) |
| F-8 | Cloud Run request logs capture full URLs (with tokens) and client IPs, contradicting §13 | Medium | Before friends (M6) |
| F-9 | "public-read or proxied" bucket also exposes `raw/` — DWD re-publication + egress bill | Medium | Before friends (M6) |
| F-10 | Byte budget and circuit breaker are a silent, day-long self-DoS; no per-response size cap | Medium | Before friends (M2) |
| F-11 | `max_rate_by_lead[k]` indexed by position, not by lead; no completeness check on the 25 members | Medium | Before friends (M4) |
| F-12 | GDPR: DPA deferral is mis-scoped; consent record has no columns; IP "hash" is reversible | Medium | Before friends (M3) |
| F-13 | `/metrics` "internal" is not expressible on Cloud Run; will ship public | Medium | Before public launch |
| F-14 | Hostile subscriber: `/forecast` `radius_m` as CPU amplifier and as a free national radar oracle | Low | Before public launch |
| F-15 | Rule parameters are a mail-amplification lever against the shared sending reputation | Low | Before public launch |
| F-16 | Web hardening gaps: `frame-ancestors`, `Referrer-Policy`, CSRF on manage forms, mail header injection | Low | Before friends (M3) |
| F-17 | Location updates silently suppress alerting for a moving user (D-17) | Informational | Before public launch |
| F-18 | Supply chain: no lockfile, unpinned deps, deploy path unspecified | Informational | Before public launch |

---

## Detailed findings

### F-1 — The upstream archive is a decompression bomb and nothing caps it

**Severity:** High · **Fix by:** before friends use it (M2)

**Design says:** §6 step 1–4 "conditional GET … decode 25 frames (numpy, in memory)"; §6.1 sizes the
job at `--task-timeout 240s` with "2 GiB" implied by §6.2 ("1 vCPU / 2 GiB"); §6 claims "the decoded
grids are 66 MB in memory". §4.3 caps *requests*, never *bytes of a single response* or bytes after
decompression. The word "bomb" does not appear in the document.

**Attack path.** The attacker needs to be able to serve one response as `opendata.dwd.de` — a
compromised mirror, a hijacked BGP path plus a mis-issued certificate, a malicious transparent proxy,
or simply DWD publishing a corrupt file (the non-malicious version of the same bug).

1. Serve `DE1200_RV_LATEST.tar.bz2` as a bz2 tar whose single member declares `size = 512 MiB`.
2. `read_frames()` (`rainalert/radar/decoder.py:134`) calls `tarfile.open(archive, "r:*")`,
   `tar.getmembers()` — which scans, and therefore decompresses, the entire stream — and then
   `handle.read()` **with no length argument** for every member.
3. `decode_frame()` only validates the payload length *after* the whole member is already resident in
   memory (`decoder.py:117`, `if len(payload) != expected`).

I built exactly this file. It is **483 bytes on the wire and expands to 512 MiB**, a ratio of
1 111 533:1. Against the current code with a 700 MB address-space limit:

```
read_frames on the 483-byte archive -> MemoryError
```

A ~2 KB archive reaches the multi-GB range. The result is an OOM-killed Cloud Run job; with
`--max-retries 1` it dies twice and then every subsequent cycle dies the same way, because `_LATEST`
keeps serving the same bytes. **No alerts for anyone, indefinitely**, and the only signal is the cycle
age alert — which is the right alert, but you will be debugging it during the rain.

Related, and true *without any attacker*: the design's "66 MB in memory" is the raw `uint16` figure.
The decoder promotes to `float32` plus a `bool` mask, which I measured at **165 MB for one complete
25-frame cycle** — before the renderer's reprojection buffers in step 7. A 2 GiB task has less
headroom than the design thinks.

**Fix.**
- Cap the compressed download: reject any response whose `Content-Length` exceeds, say, 32 MiB, and
  stream with a hard byte counter that aborts the transfer at the same limit (`Content-Length` is
  attacker-supplied too).
- Before decoding, iterate `tar.getmembers()` with limits: at most 32 members; every `member.size`
  must equal `195 + rows*cols*2` for the expected grid, or at minimum be `< 8 MiB`; sum of member
  sizes `< 128 MiB`. Reject the archive as a whole if any check fails — do not decode "the good
  parts" of a suspicious file.
- Read with a bound: `handle.read(member.size + 1)` after the size check, or wrap the fileobj in a
  counting reader.
- Keep using the in-memory path. Never `extractall()`; if a future backfill/re-render path ever
  writes members to disk, use `filter="data"` (3.12+) or validate each name is a bare basename.
- Re-size the job from the measured 165 MB + renderer, not from 66 MB.

---

### F-2 — Nothing sanity-checks the decoded field, and no cap bounds the mails one cycle can send

**Severity:** High · **Fix by:** before friends use it (M4)

**Design says:** §5 correctly insists that dimensions, precision and lead "come from the header, not
constants" — a robustness decision that is, from a security standpoint, an *unbounded trust*
decision. §9 evaluates whatever the decoder returns. §20 lists "DWD changes the product/format" as a
risk but treats it only as a correctness problem, mitigated by the golden fixture test. There is no
upper bound anywhere on how many notifications a single cycle may queue; D-9 explicitly removes all
throttling.

**Attack path (poisoned upstream, "alert everyone").**
1. Same position as F-1 — control one response.
2. Serve a well-formed 25-member archive where the whole grid reads, say, 500 (5.00 mm/5 min), with
   `missing_fraction = 0` everywhere so the §9 step-0 gate passes.
3. Every subscription in state `DRY` transitions to `WARNED` in the same cycle and a mail is queued
   for each. At the design's scale that is a handful of mails; after a public launch it is the whole
   list, delivered to real inboxes, in one cycle. It also burns the mail provider's free-tier daily
   quota (§6.2, ~300/day), which means **the genuine alerts later that day are not delivered** — the
   attack degrades the service after it stops.

**Attack path (poisoned upstream, "alert no one").** Serve a grid that is entirely the `0x29C4`
sentinel. `missing_fraction[0] = 1.0` everywhere, every subscription records `skipped_missing`,
states are frozen, and nothing is sent. The cycle-age metric is *green* the whole time, because a
cycle was successfully fetched and stored. §15's key SLI does not cover "fetched, parsed, and
meaningless". Anyone who wanted a specific person not to be warned before an outdoor event has a
five-minute window to exploit, and the service reports itself healthy throughout.

**Header-driven amplification.** `PR` is parsed as `float(b"1" + group)` (`decoder.py:91`) with the
regex `[E\-+\d.]+` and no validation. I confirmed a header carrying `PR E+20` decodes happily with
`precision = 1e+20`. Likewise `GP` accepts any `\d+x\d+`; only the payload-length equality check
constrains it, so `GP 2x1` with a 4-byte payload is a valid "national composite" as far as this code
is concerned.

**Fix.**
- Validate header fields against accepted *ranges*, not constants: `rows`/`cols` within
  `[1000, 1400]` (and warn loudly if they are not exactly 1200×1100 — format drift should page you,
  not silently reshape), `precision ∈ {1e-1, 1e-2, 1e-3}`, `interval_minutes ∈ {5}`,
  `lead_minutes ∈ [0, 120]` and a multiple of 5.
- Add a field plausibility gate before evaluation, recorded as a cycle status: reject/flag a cycle
  whose national max exceeds ~40 mm/5 min, whose wet fraction jumps implausibly versus the previous
  cycle, or whose no-data fraction leaves the observed 45–55 % band (`DWD_RV_FORMAT.md` §5, §8 give
  you real numbers to calibrate against). A flagged cycle should `skip` — freeze state — and page.
- Add a **blast-radius limiter**: if one cycle would queue notifications for more than
  `min(50 % of active subscriptions, N)` subscribers, queue nothing, record the cycle as `partial`
  and send one operator alert instead. A national squall line is real and will trip this; at your
  scale a human confirming it once is cheap, and it is the only control that bounds the worst case.
- Add a per-run global mail ceiling independent of per-subscription logic, and reserve quota so that
  confirmation mail (F-5) cannot starve alert mail.
- Pin TLS expectations as far as practical: verify the certificate chain (default), and consider
  recording and alerting on the certificate's issuer/SPKI changing, which is cheap and catches the
  realistic mis-issuance case. Do not attempt hard pinning — you will break yourself.

---

### F-3 — One bad subscription row kills the cycle for everyone

**Severity:** High · **Fix by:** before friends use it (M3/M4)

**Design says:** §6 describes the pipeline as a linear sequence of steps, §8 as a loop "for each
active subscription". Nowhere does the design say a failure in one subscription must not abort the
loop, and nowhere does it say what happens when a stored location is not on the DE1200 grid. §8.4
discusses `out_of_coverage`, but that is explicitly "inside the grid, no radar data" — a *different*
condition from "off the grid".

**Attack path.** A subscriber (or the future mobile app, or a friend abroad) sets a location the
grid code cannot handle. Measured against the current `rainalert/radar/grid.py`:

| input | result |
|---|---|
| `lat=NaN, lon=8.0` | `ValueError: cannot convert float NaN to integer` |
| `lat=91.0, lon=8.0` | `OverflowError: cannot convert float infinity to integer` |
| `lat=0, lon=0` | `OutsideGrid` (documented) |

Note that two of the three are **not** `OutsideGrid`, so a caller that carefully catches the
documented exception still dies. `NaN` is reachable through the API without any exotic tooling:
Python's `json` module accepts the bare tokens `NaN` and `Infinity`, and pydantic accepts them into a
`float` field unless `allow_inf_nan=False` is set. `lat=91` is just a typo away. `timezone` is
`text NOT NULL` with no `CHECK` (§7) and is fed to `ZoneInfo` for the quiet-hours test — an arbitrary
string raises `ZoneInfoNotFoundError` in the same loop.

Once such a row exists, it is evaluated **every cycle**, so the failure is permanent and it is not
even obvious which subscriber caused it, since §13 forbids logging exact coordinates.

**Fix.**
- Validate at the edge: `lat ∈ [47.0, 56.0]`, `lon ∈ [5.0, 16.0]` (a Germany bounding box, not the
  whole planet), `allow_inf_nan=False`, `timezone` checked against `zoneinfo.available_timezones()`,
  and a `CHECK` constraint plus an enum-ish validation in the DB for good measure. Reject at the API
  with a 422 rather than storing and failing later.
- Make `cell_of`/`radius_mask` raise `OutsideGrid` (or a common base) for *all* rejected inputs,
  including non-finite ones — a documented exception type that does not cover the actual failure
  modes is worse than none.
- Make the per-subscription loop fault-isolating by design, not by accident: wrap each subscription
  in a try/except, record `decision='error'`, increment a metric, and continue. State this in §8 so
  the implementer does not have to infer it.
- Add a "subscription is unhealthy" state surfaced in `/subscriptions/me` and `/manage`, so a user
  whose location cannot be evaluated is told, rather than silently never warned.

---

### F-4 — The long-lived API token is issued and displayed by an emailed `GET` link

**Severity:** High · **Fix by:** before friends use it (M3)

**Design says:** §10 — "`GET /confirm?token=…` … Activates the subscription, **issues the long-lived
`api` token, renders it once on the manage page**. Single use, 24 h expiry." §11 — `/confirm` "shows
the API token once with a copy button". §7 — `auth_tokens.expires_at … NULL for long-lived api
tokens`. §13 — "Transport: HTTPS only, HSTS, secure cookies (`SameSite=Lax`), CSP…". `Referrer-Policy`
is not mentioned anywhere.

**Why this is a problem, concretely.** The token that can read the subscriber's home coordinates,
move them, and delete the account never expires, and it is delivered through the single weakest
channel in the design:

1. **Link scanners and prefetchers.** Outlook SafeLinks, Proofpoint, Gmail's image/link handling and
   ordinary browser prefetch routinely issue `GET` on links in mail. Because `/confirm` is
   state-changing *and single-use*, a scanner consumes the token: the user clicks and gets "invalid
   or already used", while the scanner's response body contained the bearer token. That is both a
   functional bug (friends will hit it) and a credential disclosure to a third-party scanning
   service.
2. **`Referer` leak.** §11 puts a Leaflet/OSM map on the manage page. A page reached at
   `/confirm?token=…` that loads `https://tile.openstreetmap.org/...` sends `Referer:
   https://<host>/confirm?token=…` by default for cross-origin subresources under the default
   `strict-origin-when-cross-origin`… which sends only the origin — *but* the `/manage` link and any
   same-site navigation will carry the full URL, and any relaxation of the policy (or a third-party
   script, or an `<a>` to the attribution page) re-exposes it. Query-string secrets simply should not
   exist on a page that loads third-party resources.
3. **History, screenshots, shoulder-surfing, shared computers.** "Copy this token for the app later"
   invites friends to paste it into notes apps and chat.
4. **Logs.** See F-8 — Cloud Run logs the full request URL by default.

The same shape applies to `GET /unsubscribe?token=…`, which is **worse than a normal unsubscribe
because §13 makes it a hard delete**. A prefetching mail client irreversibly deletes the account and
the consent record. RFC 8058 exists precisely so that the one-click path is a `POST`; the design
lists `POST` as an also-accepted alternative rather than the required method.

**Fix.**
- `GET /confirm` renders a page with a button; the actual activation is a `POST` with a CSRF token.
  Same for `/unsubscribe`: `GET` shows "Confirm unsubscribe", `POST` performs it, and the
  `List-Unsubscribe-Post` header targets the `POST` endpoint only.
- Do **not** mint the long-lived API token during confirmation. Confirm the subscription; let the
  user press "create app token" on `/manage` afterwards, in a session they authenticated, and show it
  once there.
- Give API tokens a finite lifetime (e.g. 180 days) with rotation, a `last_used_at`-driven
  auto-expiry, and an actual revoke endpoint — §13 says "api tokens revocable" but §10 lists no
  endpoint that revokes one. Add `POST /api/v1/tokens/rotate` and `DELETE /api/v1/tokens/{id}`.
- Set `Referrer-Policy: no-referrer` on every page that can carry a token, and prefer tokens in a
  `POST` body or a short-lived cookie over query strings throughout.
- Separate destructive actions: one-click unsubscribe should *pause/deactivate*, and deletion should
  require the manage page. "One accidental prefetch destroys the record" is not a good property for
  the artefact that also proves consent.

---

### F-5 — Rate limiting has no defined client-IP source, and deletion erases the abuse state

**Severity:** High · **Fix by:** before friends use it (M3)

**Design says:** §10 — "Rate limits (per IP and per email hash): `POST /subscriptions` 5/hour …
Implemented in-process with a Postgres-backed counter". §13 — "the subscribe endpoint is rate limited
and only ever sends mail to an address after the double opt-in, so it cannot be used as a mail relay."

Three separate problems.

**(a) The design is silent on where "IP" comes from — and the default is wrong.** On Cloud Run the
application sees `X-Forwarded-For`, and a client may prepend its own value. An implementer who writes
`request.headers["x-forwarded-for"].split(",")[0]` — the single most common implementation — gives
every attacker an infinite supply of distinct "IPs" with one header. An implementer who uses
`request.client.host` instead rate-limits Google's front end and locks out the internet. Decide and
write it down: take the **rightmost** untrusted entry after stripping known proxy hops, verify the
actual position empirically on Cloud Run before trusting it, and treat a missing header as
"unlimited-risk" rather than "unlimited quota". Note also that per-IP limits are decoration against
IPv6: one customer /64 is 18 quintillion addresses, so the **per-email-hash** limit is the only real
control on the subscribe endpoint.

**(b) The claim in §13 is too strong.** Sending a confirmation mail to an address supplied by an
anonymous third party *is* sending unsolicited mail to that address. It is limited to 5/hour per
address, which is 120 unsolicited mails per day to a victim, each of which — critically — may contain
attacker-controlled content. The design never says what goes into the confirmation mail besides the
link; if the place name (§12, reverse-geocoded from attacker-supplied `lat`/`lon`) or any echoed
field appears there, the attacker gets a free channel to put text of their choosing into mail sent
from *your* authenticated domain. That is an abuse report and a blocklisting away from killing your
deliverability, which per §12 is the whole service.

**(c) Deleting the subscriber deletes the abuse controls.** §7 cascades everything off
`subscribers.id`, and §13 makes unsubscribe a hard delete. So: victim gets spammed → unsubscribes →
the row, the `email_hash` and (if keyed to it) the rate-limit counter vanish → the attacker
re-subscribes them immediately, with a fresh quota. There is no suppression list, and there cannot be
one if deletion is unconditional.

**Fix.**
- Pin the client-IP derivation in the design, with a note that it must be re-verified against the
  actual Cloud Run behaviour.
- Keep a minimal **suppression/abuse table** outside the cascade: `HMAC-SHA256(SECRET_KEY, email)`,
  a counter, and a "do not send confirmation mail to this address" flag, retained independently of
  the subscriber row. This is standard practice and defensible under GDPR Art. 6(1)(f) — document it
  in the privacy notice as a suppression list; it holds no plaintext address.
- Never echo user-supplied content into the confirmation mail. Resolve the place name **after**
  confirmation, not before.
- Validate the email strictly, reject anything containing CR/LF (mail header injection is live for
  the `smtp` adapter in §12), and normalise before hashing.
- Add a proof-of-work or CAPTCHA on `POST /subscriptions` before any public launch.
- Consider a global cap on confirmation mails per hour, so a distributed subscribe flood cannot
  consume the provider quota that the alert mails need.

---

### F-6 — Scale-to-zero turns unauthenticated traffic into money and into starved alerting

**Severity:** High · **Fix by:** before friends use it (M6)

**Design says:** §6 — "Cloud Run Service: api (scale to zero, min-instances 0)"; §6.1 lists the
service with "min instances 0" and no **maximum**; §10 exposes `/healthz`, `/readyz`
("readiness = DB reachable"), `/metrics` and `/api/v1/overlays/timeline` with auth "none"; §6.1
chooses Neon free tier by default (Q-3).

**Attack path.** No credentials needed.
1. Fire a few hundred concurrent requests at `GET /readyz`.
2. Cloud Run scales out — with the default `max-instances` of 100 if nothing is set — and **each
   instance opens a database connection on every readiness check** because readiness is defined as
   "DB reachable".
3. Neon's free tier has a modest connection ceiling and an autosuspend the flood defeats. Connections
   are exhausted by the web tier.
4. The **ingest job** now cannot get a connection. It cannot take its `pg_try_advisory_lock`, cannot
   write `radar_cycles`, cannot queue notifications. Alerting stops, and it stops because of
   unauthenticated traffic to a *health endpoint*.
5. Concurrently, the operator pays for 100 instances of vCPU/memory for as long as the flood lasts,
   against a design whose expected API bill is "~0".

The same lever exists on `/api/v1/overlays/timeline` (unauthenticated, a DB query over 144 rows per
call, `Cache-Control 60 s` protects the client, not the server) and — per F-5 — on the Postgres-backed
rate limiter itself, which is an unauthenticated write primitive into the database.

**Fix.**
- Set `--max-instances` explicitly and low (3–5 at this scale). This is the single highest-value line
  in the whole deployment; write it into §6.1 as a hard requirement with the rationale, not as a
  default.
- Make `/readyz` cache its DB check for ~10 s, and return the cached verdict; `/healthz` must not
  touch the DB at all.
- Give the ingest job its **own database role and its own connection budget**, and reserve
  connections for it (`ALTER ROLE ingest CONNECTION LIMIT`, or separate pooler endpoints), so the web
  tier can never starve alerting. Alerting availability must not depend on web-tier behaviour.
- Put a CDN/cache in front of the timeline manifest, or serve it as a static object from GCS written
  by the job.
- Set a billing budget alert — not a control, but the only thing that tells you within hours.

---

### F-7 — The nominal time comes from the upstream header and drives the staleness SLI

**Severity:** Medium · **Fix by:** before friends use it (M2)

**Design says:** §4.4 — "the nominal time comes from the file header (`_LATEST` carries no timestamp
in its name), and `radar_cycles` dedupes on it"; §7 — `nominal_time timestamptz NOT NULL UNIQUE`;
§15 — "`rainalert_cycle_age_seconds` — **the key SLI**: now − latest `radar_cycles.nominal_time`.
Alert if > 20 min."

`DWD_RV_FORMAT.md` §2 justifies reading the header rather than the filename, and that reasoning is
correct. The security consequence was not drawn: **the value that decides whether your monitoring
believes the service is healthy is supplied by the remote party**, unvalidated.

**Attack path.** One poisoned or corrupt archive with a nominal time of, say, `2027-01-01`:
- `radar_cycles` accepts it (`UNIQUE` only prevents duplicates).
- `cycle_age_seconds` becomes negative — "the freshest data we ever had". The 20-minute staleness
  alert, the design's primary safety net, will not fire again until real time catches up.
- Ingestion keeps running, every subsequent real cycle is older than the poisoned one, and the
  timeline manifest (§11.1) orders frames by a bogus offset.

The non-malicious version — a DWD clock/format glitch producing a date in 2099, or my measured case
where a malformed day field raises a bare `ValueError` (`decoder.py:83`, `day is out of range for
month`) rather than `RVFormatError` — is at least as likely.

**Fix.**
- Reject any cycle whose header nominal time is more than ~15 minutes in the future or ~3 hours in
  the past relative to the job's clock, and page rather than store.
- Cross-check the header time against the `Last-Modified` header of the fetched `_LATEST` object
  (`DWD_RV_FORMAT.md` §4 shows they track each other to within ~5 minutes); disagreement beyond that
  is an integrity signal.
- Make `cycle_age_seconds` clamp at zero and alert on *negative* values as a distinct condition.
- Convert every parse failure in the header path into `RVFormatError` (currently `ValueError` from
  `datetime()` and `int()` escape the documented type — same class of bug as F-3).

---

### F-8 — Cloud Run's own request logs defeat §13's logging rules

**Severity:** Medium · **Fix by:** before friends use it (M6)

**Design says:** §13 — "no IP logs beyond a hashed value for rate limiting, retained 7 days" and
"Logging: never log full email addresses or exact coordinates". §15 specifies the application's own
structured logs carefully.

**The gap.** Cloud Run writes a `run.googleapis.com/requests` entry for every request, automatically,
containing `httpRequest.remoteIp` (the real client IP) and `httpRequest.requestUrl` — **the full URL
including the query string**. With `/confirm?token=…` and `/unsubscribe?token=…` (F-4), that means
live credentials sit in Cloud Logging, default retention **30 days**, readable by anyone holding
`roles/logging.viewer` or `roles/viewer` on the project. The design's privacy claim is therefore not
true as deployed, and the token hygiene of §13 ("plaintext never stored") is quietly undone by the
platform.

**Fix.**
- Move tokens out of query strings (F-4). This fixes the token half completely and is the real fix.
- Configure a log sink/exclusion filter that drops or redacts `run.googleapis.com/requests`, or set
  a short retention (e.g. 7 days) on the default bucket to match the stated policy, and say so in §13
  rather than describing only the application's own logging.
- Do the same for the rendered `Referer` and `User-Agent` fields if you ever enable extended logging.
- Correct §13 to describe the platform's logging, not just the application's. As written, an
  implementer will reasonably believe the requirement is met when it is not.

---

### F-9 — "public-read or proxied" also exposes the raw DWD archives

**Severity:** Medium · **Fix by:** before friends use it (M6)

**Design says:** §6 diagram — "GCS (overlay PNGs, public-read or proxied)"; §6.1 — a **single** bucket
`rainalert-data` holding `raw/` (48 h), `overlays/obs/`, `overlays/fc/`, with "uniform ACL".

Uniform bucket-level access is the right call, but it is exactly what makes the ambiguity dangerous:
the natural way to make overlay PNGs public with uniform access is
`gsutil iam ch allUsers:objectViewer gs://rainalert-data`, which is **bucket-wide**. That publishes
`raw/` too, and the object names are deterministic (`raw/DE1200_RV<YYMMDDHHMM>.tar.bz2`), so no
listing permission is needed to enumerate 576 archives.

Consequences: (a) it is a public mirror of DWD bulk data, contradicting the document's own
anti-requirement in §1 — "Do **not** mirror or re-publish DWD bulk data"; (b) every GET is billed
egress against your project — ~1.4 GB of archives sitting there at all times, fetched in a loop, is
an inexpensive way for a stranger to spend your money; (c) the overlays themselves are then trivially
hotlinkable at your cost.

The overlay images contain no personal data — that part is fine and worth keeping.

**Fix.**
- Two buckets: `rainalert-private` (raw archives, never public) and `rainalert-public` (overlays
  only). Decide "public-read **or** proxied" rather than leaving both in the design.
- If public: serve overlays through Cloud CDN with a cache, disable object listing, and set a
  lifecycle so nothing lingers beyond §6.1's TTLs. If proxied: the proxy must cache, or it becomes
  the F-6 amplifier.
- Set a per-project egress budget alert.

---

### F-10 — The byte budget and circuit breaker are a silent, day-long self-DoS

**Severity:** Medium · **Fix by:** before friends use it (M2)

**Design says:** §4.3 rule 7 — "Enforce a daily byte budget guard (`DWD_DAILY_BYTE_BUDGET`, default
8 GiB). Exceeding it **logs an error and halts ingestion** rather than silently hammering."
§4.3 rule 6 — "after 5 consecutive failed cycles, open a circuit breaker (stop fetching for 15 min)".

Both controls are correct in intent — being a good citizen towards DWD is a genuine requirement — but
they are **fail-silent in the direction of "no warnings"**, and their trigger is partly controlled by
the other side.

**Attack path / failure path.** There is no cap on the size of a single response (F-1). One response
of 8 GiB — or a handful of oversized ones, or a retry bug during a storm — exhausts the daily budget.
Ingestion then halts **for the rest of the UTC day**: up to 24 hours with no alerts, from a single
bad response. The document says it "logs an error"; §15's operator alerts list "daily budget > 80 %",
which is good, but the halt itself is not listed as a paging condition, and the state does not appear
in `/healthz`, `/readyz` or the UI. Users see a map that says the data is stale (§11.1) only if they
happen to visit the page.

**Fix.**
- Cap a single response (F-1) so no one request can consume a meaningful fraction of the budget.
- Make the budget *per-hour* as well as per-day, so exhaustion costs an hour, not a day.
- Make budget exhaustion and an open circuit breaker **page the operator immediately** and surface as
  `readyz`-degraded plus a banner in the UI, rather than a log line. §15 already has the circuit
  breaker as an operator alert; add the budget halt with the same status.
- Record the reason in `radar_cycles.status`/`notes` so the timeline gap has an explanation.

---

### F-11 — `max_rate_by_lead[k]` is positional; a partial archive shifts every lead time

**Severity:** Medium · **Fix by:** before friends use it (M4)

**Design says:** §7 — `max_rate_by_lead real[] NOT NULL, -- 25 values, mm per 5 min, **index = frame
k**`; §9 — `hits := { k : 1 <= k <= L/5 and max_rate_by_lead[k] >= threshold }` and
`predicted_start_at = T0 + 5·first_hit`. §16.1 explicitly tells the tests **not** to assert "25
members", because the fixtures hold three.

The decoder returns a *list* sorted by `(nominal_time, lead_minutes)` (`decoder.py:145`), whose
length equals however many members the archive contained. The design then indexes that list by
position and multiplies the index by 5 to get minutes. Those two things agree only when all 25
members are present and their leads are exactly 0, 5, …, 120.

If DWD (or an attacker, or a truncated transfer) delivers 24 members with `_015` missing, every
subsequent entry shifts down one slot: rain at +60 is reported and *emailed* as rain at +55, and the
`rain_events.predicted_start_at` written to the permanent record is wrong — which then corrupts the
verification job (§9) that exists to tune the thresholds. With an attacker-forged `VV` field, the
misalignment is arbitrary and chosen.

This is a small bug with disproportionate reach because it silently degrades the one number the
product exists to produce, and because the test suite is explicitly instructed not to assert on the
member count.

**Fix.**
- Key the sample series by `lead_minutes`, not by position: build `dict[int, float]` and materialise
  the 25-slot array from it, leaving `NULL`/`NaN` for absent leads (which the §9 per-frame gate
  already knows how to treat as "excluded, not dry").
- In the ingest path (as opposed to the fixture-driven tests) **do** assert completeness: leads must
  be exactly `range(0, 125, 5)`, all with the same `nominal_time`. A cycle that is not complete is
  `status='partial'` and is evaluated with the missing leads gated out, not silently compacted.
- Add a regression test with a deliberately gappy archive — the existing fixtures make this cheap.

---

### F-12 — GDPR: the DPA deferral is mis-scoped, the consent record has no columns, the IP hash is reversible

**Severity:** Medium · **Fix by:** before friends use it (M3)

**Design says:** §13 — "Legal basis: consent (Art. 6(1)(a)), obtained via double opt-in; the
confirmation timestamp and **source IP hash are the consent record**"; and under "Deferred until a
public launch (D-18): Impressum …, full Datenschutzerklärung, **Auftragsverarbeitungsvertrag with the
mail provider**".

Three distinct issues.

**(a) The DPA is not deferrable.** Art. 28(3) GDPR requires a contract with any processor from the
first processing operation. The moment a friend's address goes to Brevo/Mailgun/SendGrid, that
provider is your processor. All three offer a click-through DPA; it takes ten minutes. The same
applies to Google Cloud (covered by the standard Cloud Data Processing Addendum — accept it) and to
Neon if you choose it over Cloud SQL (Q-3). This one is cheap to fix and awkward to explain if it is
not. Impressum and the full Datenschutzerklärung *are* reasonably deferred for a genuinely private
service; the DPA is not. Separately: if you pick a US-based mail provider, note the transfer basis in
the same breath.

**(b) The consent record does not exist in the schema.** §7's `subscribers` has `confirmed_at` and
nothing else — no IP hash column, no consent text version, no record of *what* was consented to. §13
describes an artefact the data model cannot hold, and Art. 7(1) requires you to be able to
demonstrate consent. Add `consent_ip_hmac bytea`, `consent_at`, `consent_text_version text`,
`consent_user_agent text` (optional). Note the tension with F-4: hard-deleting on unsubscribe also
destroys your proof that consent was ever given — which is correct under Art. 17 but means you should
keep an *anonymous* aggregate or the suppression entry from F-5, not the record itself.

**(c) The "hash" is not a safeguard.** `sha256(ip)` over IPv4 is a 32-bit keyspace — exhaustible on a
laptop in seconds; IPv6 is not much better in practice given prefix structure. §13 presents the hash
as a data-minimisation measure. Use `HMAC-SHA256(SECRET_KEY, ip)` truncated to 8–16 bytes, with the
key in Secret Manager, and say in the privacy text that it is a keyed hash. The same argument applies
to `subscribers.email_hash`, though there it matters less because the plaintext address is in the
adjacent column anyway — worth noting so nobody mistakes it for protection.

**(d) Deletion does not reach everywhere.** `DELETE /subscriptions/me` cascades within Postgres, but
the mail provider retains delivery logs (recipient address, subject, often body) for 30+ days, and
Cloud Logging holds request logs (F-8). The privacy text must say so, and the deletion runbook should
include the provider's log-purge/anonymisation setting where one exists.

**What is genuinely good here** and should not be diluted: D-23's reasoning that an indefinite
5-minute-resolution per-subscriber series is a presence log is exactly right, and the 48 h TTL is the
correct conclusion. Keep it.

---

### F-13 — `/metrics` "internal" is not expressible on Cloud Run

**Severity:** Medium · **Fix by:** before public launch (earlier if the URL is guessable, which it is)

**Design says:** §10 table — "`GET /metrics` | **internal** | Prometheus-format metrics (§15)".

**The gap.** Cloud Run IAM is per-*service*, not per-path. The web UI must be reachable by
`allUsers`, therefore every path on that service is reachable by `allUsers`, including `/metrics`. The
word "internal" in the design has no mechanism behind it, and the default outcome is a publicly
readable metrics endpoint exposing `rainalert_subscriptions_active` (how many friends you have),
`rainalert_notifications_total{status}`, budget-used ratio, pipeline timings and missing-fraction
histograms. None of that is catastrophic; it is a free reconnaissance and health oracle, and it tells
an attacker in real time whether the attacks in F-2/F-6/F-10 are working.

**Fix.** Pick one and write it down: (a) require a bearer token/static credential on `/metrics`
enforced in the app; (b) bind metrics to a separate Cloud Run service with
`--no-allow-unauthenticated` and IAM; or (c) drop `/metrics` entirely and push to Cloud Monitoring,
which §15 says you mirror to anyway — this is probably the simplest and matches §1's guiding
principle. Same question applies to `/healthz` and `/readyz`: keep them, but make them say as little
as possible to an anonymous caller.

---

### F-14 — A hostile subscriber: `/forecast` as CPU amplifier and as a free national radar oracle

**Severity:** Low · **Fix by:** before public launch

**Design says:** §10 — "`GET /forecast?lat=&lon=&radius_m=` | api | The 25 sampled values for an
arbitrary point + a human summary"; rate limit "`GET /forecast` 120/hour". §7's `CHECK (radius_m
BETWEEN 0 AND 20000)` constrains the *column*, not this *query parameter*, and the design does not say
the endpoint validates it.

**Attack path.** With one valid token: `GET /forecast?lat=50.1&lon=8.68&radius_m=5000000`. In the
current `radius_mask()` implementation the search box is clipped to the grid, so the call does not
explode — it just does the maximum possible work. Measured:

```
radius_mask(50.1, 8.68, 5_000_000) -> 1 320 000 cells, 1.55 s
```

That is the entire national grid, ~1.5 s of CPU and tens of MB of temporary arrays per request, from
a request that costs the attacker nothing. At 120 requests/hour that is 3 CPU-minutes/hour per token
— not ruinous, but it is a 100× amplifier in a pay-per-use deployment, and combined with F-6 (no
instance cap) it scales with however many tokens the attacker can create.

Secondly: `/forecast` accepts an *arbitrary* point. With 120 requests/hour a client can walk the grid
and reconstruct the national precipitation field — which is a re-publication of DWD data through your
endpoint (§1: "Do not mirror or re-publish DWD bulk data") at your compute and egress cost.

**Fix.** Validate `radius_m` at the endpoint against the same 0–20 000 m bound as the column, and
`lat`/`lon` against the Germany box from F-3. Consider restricting `/forecast` to the caller's own
subscription location plus a small offset, or dropping the arbitrary-point capability from the public
API and keeping it in the CLI (`rainalert probe`) where it belongs for debugging. If you keep it,
lower the rate limit and attach it to the subscriber, not just the IP.

---

### F-15 — Rule parameters are a mail-amplification lever against a shared reputation

**Severity:** Low · **Fix by:** before public launch

**Design says:** D-9 — "v1 throttling: **none beyond the state machine** — maximum notifications, for
debugging"; D-10 — `min_gap_minutes` defaults to `0` (off); §10 — `PATCH /subscriptions/me` lets the
holder set `radius_m`, `threshold_mm_5min`, `lead_time_minutes`, quiet hours. §7's constraints are
`threshold_mm_5min > 0` (no upper bound and, at `numeric(5,2)`, a floor of 0.01),
`lead_time_minutes BETWEEN 5 AND 120`, `radius_m BETWEEN 0 AND 20000`.

**Attack path.** A legitimate token holder sets `threshold_mm_5min = 0.01`, `lead_time_minutes = 120`,
`radius_m = 20000`. Each of those individually is within spec; together they mean "warn me if a
20 km-radius disc contains the faintest radar echo any time in the next two hours", which in German
autumn is close to "always". With throttling off by design, the state machine still limits to one
mail per dry→warned event — but the definition of "event" has been widened so far that the account
generates mail on most cycles during unsettled weather.

Why this matters beyond the attacker's own inbox: **the mail quota and the sending domain's
reputation are shared**. §6.2 budgets the provider free tier at ~300 mails/day for everyone. One
account generating dozens of mails a day, especially if the recipient then marks them as spam,
degrades deliverability for every other subscriber — and §12 says deliverability is the difference
between a working service and a pointless one.

**Fix.** Add a hard per-subscription ceiling independent of the rule parameters — e.g. at most 12
alert mails per rolling 24 h, recorded as `suppressed_cap` so it is visible in `evaluations` — and a
global per-day ceiling (F-2). Tighten the parameter ranges to defensible values
(`threshold >= 0.05`, `lead <= 60` given the nowcast skill argument in D-13 itself). Keep D-9's
"no throttling" as the *default*, which is a reasonable debugging choice; just do not let it be
unbounded.

**Status (2026-09-20): partially addressed, deliberately.** `/manage` ships with bounds enforced
at the edge and in the schema (D-28), and `radius_m` keeps its 20 km ceiling. The two
tightenings this finding asked for were **not** taken, by the product owner's decision: the
threshold floor is 0.01 rather than 0.05, and the lead ceiling is 120 rather than 60. The
reasoning on both is that the bound should come from the data — 0.01 is the smallest value RV
can express, 120 is the whole forecast it carries — rather than from a guess about behaviour.

That leaves the amplification path in this finding **open**, and narrower only in that
`threshold=0.01, lead=120, radius=20000` is still reachable. The remaining half of the fix is
the part that actually bounds the damage and does not narrow the knobs: a per-subscription cap
on alerts per rolling 24 h, and the global per-day ceiling from F-2. Neither is built. Both
should land before anyone but the author is a subscriber — which is also when the shared sending
reputation this finding is about starts to exist.

---

### F-16 — Web hardening gaps the design does not mention

**Severity:** Low · **Fix by:** before friends use it (M3)

**Design says:** §13 — "HTTPS only, HSTS, secure cookies (`SameSite=Lax`), CSP without inline scripts
except a nonce for the map bootstrap". That is a good start and above average. Not mentioned:

- **Clickjacking.** `/manage` has pause/resume and **delete** controls. No `X-Frame-Options` or
  `Content-Security-Policy: frame-ancestors 'none'`. An attacker's page can frame `/manage` and
  trick a logged-in subscriber into deleting their subscription. Add `frame-ancestors 'none'` to the
  CSP you are already writing.
- **CSRF.** `SameSite=Lax` blocks cross-site `POST` with cookies, which covers most of it — but the
  design's state-changing actions are `GET`s (F-4), which `Lax` deliberately does *not* protect.
  Convert those to `POST` and add a per-session CSRF token to the manage forms. Say so in §13; an
  implementer reading "SameSite=Lax" alone will conclude CSRF is handled.
- **`Referrer-Policy`.** See F-4. `no-referrer` on token-bearing pages.
- **Mail header injection.** §12 includes a generic `smtp` adapter. An email address or a
  reverse-geocoded place name (Q-7) containing `\r\n` becomes extra SMTP headers — a `Bcc:` of the
  attacker's choosing. Validate and strip CR/LF on every value that reaches a header, including the
  subject line's place name.
- **Third-party map tiles.** §11 loads OSM tiles directly in the browser, which discloses every
  subscriber's approximate location to a third party on every map view and is also why a cookie
  banner would be needed at public launch (already noted as Q-5/Q-2 — good). Worth stating that the
  *privacy* consequence, not just the usage policy, is why it must change.
- **Cookie/session model is undefined.** §7 has no sessions table and §13 mentions cookies without
  saying what is in them or what signs them. `SECRET_KEY` (§14) is described as "token/HMAC signing"
  but §13 says tokens are random and stored hashed, so `SECRET_KEY` currently signs nothing
  identified. This silence is how homemade signed-cookie schemes get written. Decide: either the
  manage page is authenticated by a short-lived signed cookie set after a magic-link `POST`, or by a
  `manage` token in the URL — and if the latter, state its lifetime, because a manage link in every
  alert email is a bearer credential to someone's home coordinates sitting in their inbox forever.

**Status (2026-09-20).** Decided and built, as the first of the two options (D-25 … D-27, §11.2):

- **Clickjacking** — fixed earlier: `frame-ancestors 'none'` and `X-Frame-Options: DENY` are on
  every response.
- **CSRF** — a cookie-authenticated write requires a CSRF value that was rendered into the page
  and is echoed in a custom header; a bearer-authenticated write does not need one and never
  could be forged cross-site. The value is signed with the purpose inside the MAC, so a session
  token and a CSRF token for the same subscriber are not interchangeable, and it is checked
  against *this* session rather than merely being a valid signature.
- **`Referrer-Policy`** — `no-referrer` site-wide; the tile layer overrides it per element to
  `strict-origin-when-cross-origin`, which sends an origin and never a path or query.
- **Cookie/session model** — a signed, stateless cookie: `session.<id>.<expiry>.<mac>`, HttpOnly,
  `SameSite=Lax`, 30 minutes, `Secure` when `PUBLIC_BASE_URL` is https. No session table. The
  magic link that sets it is a **stored** token, so it can be single use, and lives 15 minutes.
  Rotating `SECRET_KEY` ends every session and every unsubscribe link at once, which is the only
  revocation this needs.
- **Mail header injection** — unchanged: `OutboundMessage` rejects CR/LF in `to`, `subject`,
  `click_url` and every header value.
- **Third-party map tiles** — unchanged, still Q-5/Q-2, and `/manage` loads Leaflet from the same
  CDN as `/map` (M6: vendor it). Scripts and styles come from there; **images do not**, and that
  asymmetry is deliberate. Leaflet's default marker is a PNG it fetches from wherever the library
  came from, `img-src` does not list the CDN, and the browser refused it — correctly, though the
  visible result was a broken-image placeholder until the marker was redrawn as an inline SVG
  `divIcon` (2026-09-21). Allow-listing the CDN for images would have been the other fix and is
  the wrong one: an image request is a page view reported to a third party, on a map showing
  somebody's home.

---

### F-17 — Location updates silently suppress alerting for a moving user

**Severity:** Informational (but it will bite a real user) · **Fix by:** before public launch

**Design says:** D-17 / §9 — "any | location moved > 1 km (D-17) | `UNKNOWN` | no"; §10 —
`PUT /subscriptions/me/location` "Rate limited to 1 per 60 s"; §9's table — from `UNKNOWN` the only
transitions are to `DRY` or `RAINING`, **neither of which mails**.

Therefore a subscription whose location moves more than 1 km between consecutive cycles never reaches
`WARNED` and never receives an alert. That is correct per-transition and wrong end-to-end: the mobile
app the whole API is shaped for (§1, D-15) pushes GPS, and a user commuting, cycling or on a train
moves more than 1 km every five minutes. They will never be warned, and nothing tells them so. An
attacker does not need to be involved; but note that it is also a *deliberate* self-suppression
mechanism, and if you ever add a shared/family location it becomes a way to suppress someone else's
alerts.

**Fix.** Separate "the state is stale because the location changed" from "we know nothing": on a
jump, re-seed the state from the current cycle's `now_wet` at the *new* location in the same
evaluation rather than parking in `UNKNOWN` for a cycle. Surface "moving too fast to warn" in
`/subscriptions/me` so the app can say so. Consider rate-limiting *effective* location changes rather
than API calls.

---

### F-18 — Supply chain and deploy path

**Severity:** Informational · **Fix by:** before public launch

**Design says:** §16 — "CI: ruff + mypy (strict on `rainalert/`) + pytest + the import-linter rule …
Container image built and smoke-tested"; §17 lists `infra/` as "terraform or gcloud scripts"; §6.1
lists Artifact Registry. Nothing states who may deploy, how CI authenticates to GCP, or how
dependencies are pinned.

Observed in the repo: `pyproject.toml` pins nothing (`numpy>=1.26`, `pyproj>=3.6`, `wradlib>=2.0`),
there is no lockfile, and there is no `mypy` or import-linter configuration despite §16 promising
both (the import check is implemented as an AST test in `tests/test_packaging.py`, which is a neat
solution — but it is not what the design says, and it does not catch a dynamic import).

The realistic risk is not a targeted attack; it is (a) an unpinned transitive dependency shipping a
compromised release into an image that holds your DB credentials and mail API key, and (b) a GitHub
Actions deploy configured with a long-lived service-account JSON key in a repo secret, which is the
default thing people do and is a permanent key to the whole project.

**Fix.** Commit a lockfile (`uv.lock`/`requirements.txt` with hashes) and install with
`--require-hashes` in the Dockerfile. Pin the base image by digest, run as non-root with a read-only
root filesystem. Use **Workload Identity Federation** for CI → GCP, never a JSON key. Give the job
and the service **separate service accounts** with least privilege — the API service needs DB access
and GCS read; the ingest job needs GCS write and Secret Manager; neither needs project Editor, which
is what the default compute service account carries. That separation is what limits the damage if the
public-facing container is ever compromised, and the design currently does not mention service
accounts at all beyond "OIDC SA" for the scheduler. Mount secrets as **Secret Manager volumes rather
than env vars** where you can, and mark them `SecretStr` in the pydantic `Settings` object (§14) so
that a debug log or an exception page cannot print `DATABASE_URL` and `MAIL_API_KEY` — with a plain
pydantic model, `repr(settings)` does exactly that.

---

## What the design already gets right — do not change these

These are deliberate decisions that are stronger than the norm, and several of them pre-empt classic
vulnerabilities. They should survive any remediation.

- **The API is `/me`-shaped throughout.** Every subscriber-scoped endpoint resolves the subject from
  the bearer token; no endpoint takes a subscription id. That removes the entire IDOR class, which is
  the single most common vulnerability in services of this shape. Keep it — resist any future
  `GET /subscriptions/{id}` "for the admin UI".
- **Enumeration-safe subscribe.** §10: "Always responds identically whether or not the address is
  already known". Correct, and §16.7 tests it. (Watch the timing side channel if you ever add a
  synchronous DB lookup before the response, but at this scale it is not worth engineering.)
- **Token handling.** 32 bytes from `secrets.token_urlsafe`, stored only as SHA-256, constant-time
  comparison, single-use confirm tokens with 24 h expiry (§13). The storage model is right; F-4 is
  about the *delivery channel*, not the tokens themselves.
- **Double opt-in with a consent trail as the legal basis**, RFC 8058 one-click unsubscribe, and "no
  marketing mail, ever" (§12, §13). This is the correct posture and most hobby projects skip it.
- **The no-data sentinel handling.** §5 step 4 and `DWD_RV_FORMAT.md` §8 catch a bug
  (`0x29C4 & 0x0FFF = 2500` → 25 mm/5 min across 47 % of the grid) that would have alerted every
  subscriber forever. It is pinned by a required test. This is the single best piece of engineering in
  the repository.
- **The per-frame missing gate**, driven by a *real* observed outage (Borkum, `DWD_RV_FORMAT.md` §5)
  rather than a hypothetical, with a committed fixture. Radar outage never reads as "dry" and never
  clears a `WARNED` state. That is fail-safe design in the right direction.
- **Georeferencing verified against wradlib over all 1 320 000 cells**, with the corner-vs-centre and
  row-0-is-south conventions written down. A silent few-cell offset is undetectable by eye and would
  warn the wrong village; this is properly closed.
- **The data-minimisation reasoning in D-23/§8.1** — recognising that a per-subscriber 5-minute series
  is a presence log and capping it at 48 h. That is a privacy decision made for the right reason, not
  a storage decision that happened to help.
- **Politeness toward `opendata.dwd.de`** (§4.3): one request per cycle, conditional GETs, attempt
  caps, backoff with jitter, circuit breaker, no directory crawling, descriptive User-Agent. Third-party
  abuse is a security property too, and this is more disciplined than most commercial ingesters.
  (F-10 is about how those controls fail, not about whether they should exist.)
- **Job concurrency 1 plus a Postgres advisory lock** (§6.1) and the queue-then-deliver transaction
  boundary (§6) — double-send is designed out rather than hoped away.
- **No tar extraction to disk.** `read_frames()` reads members through `extractfile()` and skips
  anything that is not `isfile()`, so tar path traversal, absolute paths and symlink attacks — the
  usual first thing to look for in an archive parser — do not apply. Write this down as a rule in §5
  so that the backfill/re-render path (§11.1) does not reintroduce `extractall()` later.
- **`state_machine.py` and `rules.py` must be pure** (§17). Pure, injectable-time decision logic is
  what makes the table-driven tests in §16.3 trustworthy, and it is also what will let you write
  adversarial test cases for F-2 and F-11 cheaply.
- **Staleness as a first-class SLI** (§4.4, §15) with the explicit rule that "a stale cycle must never
  be silently treated as no rain". The instinct is right; F-7 is only about the input to that metric
  being attacker-influenced.

---

## Where the design is silent rather than wrong

Collected for convenience — these are the places an implementer will fill in with a bad default:

| Gap | Likely bad default | Finding |
|---|---|---|
| Source of the client IP for rate limiting | `X-Forwarded-For.split(",")[0]` — attacker-controlled | F-5 |
| What "`/metrics` — internal" means on Cloud Run | public, because per-path IAM does not exist | F-13 |
| Per-subscription error isolation in the cycle loop | one exception aborts the run | F-3 |
| Maximum instances on the API service | platform default of 100 | F-6 |
| Whether the overlay bucket is public-read *or* proxied | `allUsers:objectViewer` on the whole bucket | F-9 |
| Size limits on the upstream response and its members | none | F-1, F-10 |
| Validation ranges for header fields taken "from the header, not constants" | none | F-2 |
| Service accounts for the job and the service | shared default compute SA with Editor | F-18 |
| ~~What the session cookie contains and what `SECRET_KEY` signs~~ | ~~a homemade signed-cookie scheme~~ | F-16 — **answered** by D-25…D-27 (§11.2) |
| Contents of the confirmation mail | echoes user input to a third party's inbox | F-5 |
| ~~Lifetime and scope of the `manage` token in emailed links~~ | ~~long-lived bearer URL in every alert mail~~ | F-16 — **answered**: 15 min, single use, requested on demand, never attached to an alert |

---

## Suggested order of work

1. **F-3, F-1, F-11** — cheap, local to code you are about to write in M2/M4, and each one prevents a
   total alerting outage.
2. **F-4, F-5, F-16** — all land in M3 with the API; fixing the confirm/unsubscribe flow shape first
   avoids a migration later.
3. **F-6, F-8, F-9, F-18** — M6 deployment configuration; `--max-instances` and the bucket split are
   single lines with outsized value.
4. **F-2, F-10, F-7** — the upstream-trust cluster; do the plausibility gate and the blast-radius cap
   together in M4.
5. **F-12** — accept the DPAs now; the schema columns can come with the M3 migration.
6. **F-13, F-14, F-15, F-17** — before any public link exists.

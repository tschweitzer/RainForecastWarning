# RainAlert — Design & Requirements

**Status:** Draft v2, ready for implementation
**Date:** 2026-09-16
**Changes since v1:** timeline slider extended to −12 h … +2 h (D-22); evaluation storage reduced to a
48 h debug log (D-23, §8.1); "simplicity over optimization" added as a governing principle (§1); the
RV format verified against real data (M0) and the design corrected accordingly; an adversarial
security review folded in (§4.3.1, §8.0, §9, §15, §18.1); database resolved to Cloud SQL (§6.3)
**Repository:** RainForecastWarning — the Python package/module namespace is `rainalert`
**Audience:** the coding agent that will implement this, and future-me

> **Repository.** `github.com/tschweitzer/RainForecastWarning`. This document is the authoritative
> specification until code exists; keep it updated as decisions change.

---

## 1. Problem statement

Warn a user shortly *before* it starts raining at their location in Germany, by email, using DWD
radar nowcast data. "Get the laundry in / take a jacket" — not a general weather app.

### In scope for v1
- Server-side service, deployed to Google Cloud, that ingests DWD radar nowcast data every 5 minutes.
- Email subscription with double opt-in, one location per subscriber, location updatable via API.
- Alert when rain is predicted to start at the subscriber's location within the next 30 minutes.
- Minimal server-rendered web UI: subscribe (map picker), confirm, manage, unsubscribe.
- The map picker page renders a rain **timeline**: an overlay with a slider spanning the last
  **12 hours of observed rain** through the next **2 hours of nowcast**.
- REST API shaped so a future mobile app can use it unchanged.

### Explicitly out of scope for v1
- Mobile apps (Android/iOS) and push notifications (FCM/APNs). A `Notifier` interface exists; the
  push adapter is a stub that raises `NotImplementedError`.
- Countries other than Germany / data sources other than DWD.
- User accounts with passwords, OAuth, multi-tenancy, teams.
- Snow/hail/thunderstorm classification, wind, temperature.
- "All clear" / "rain has stopped" notifications.
- Public launch paperwork (Impressum, full Datenschutzerklärung, DPA). See §13.

### Guiding principle

**Simplicity over optimization.** Where a simpler design and a cleverer one both work at the scale
this service actually operates at (tens of subscribers, one radar cycle every five minutes), take the
simpler one — fewer tables, fewer columns, fewer moving parts — even if it leaves performance or
completeness on the table. Optimize when a measurement says to, not in advance. This rule outranks
any efficiency argument elsewhere in this document.

### Non-goals / anti-requirements
- Do **not** mirror or re-publish DWD bulk data.
- Do **not** poll DWD per subscriber. One fetch per cycle, globally.
- Do **not** build an SPA or a build toolchain for the frontend.

---

## 2. Decisions log

Decisions taken during the requirements interview. Each is binding unless superseded.

| # | Decision | Rationale / note |
|---|---|---|
| D-1 | Own new repository: `tschweitzer/RainForecastWarning` | Created 2026-09-16; this doc is its first commit |
| D-2 | Trigger = "rain starts within lead window **and** it is currently dry at the location" | "Get the laundry in" semantics; avoids firing during ongoing rain |
| D-3 | Spatial rule = max over cells within a radius (default **2000 m**) | 1 km grid + nowcast advection error; a single cell is too jittery |
| D-4 | Host on GCP, serverless, scale-to-zero | Cloud Run service + Cloud Run job + Cloud Scheduler + GCS |
| D-5 | Identity = email + magic link (double opt-in); subscription holds an opaque API token | GDPR consent trail, no passwords, reusable by the future app |
| D-6 | Ingestion stores **full grids** (the original archives), not just samples | Enables replay, debugging, and the map overlay feature |
| D-7 | Retention: raw archives **48 h**; forecast overlays **1 h**; observed overlays **14 h**; `evaluations` **48 h**; `rain_events` / `notifications` indefinitely | See D-23 |
| D-8 | Alert de-duplication via a **per-subscription state machine** (§9), not a fixed cooldown | One mail per rain *event*, not per cycle |
| D-9 | v1 throttling: **none beyond the state machine** — maximum notifications, for debugging | Explicit user choice |
| D-10 | `min_gap_minutes` ("only once per N minutes") and quiet hours exist in the schema and config now, default **off** (`0` / disabled) | Future-configurable without migration |
| D-11 | Language: **Python everywhere** (FastAPI + Jinja2 templates, numpy) | Radar tooling is Python; one image, one language |
| D-12 | Mail via a pluggable `Notifier`; default adapter a transactional provider (Brevo/Mailgun/SendGrid free tier); console adapter for dev | Deliverability; swappable via config |
| D-13 | Defaults: **lead time 30 min**, threshold **> 0.1 mm / 5 min** | Nowcast skill decays fast; 0.1 mm/5 min ≈ 1.2 mm/h ≈ "you get wet" |
| D-14 | Alert rule parameters are **per-subscription columns with defaults**, not constants | v1 UI shows defaults only; later UI edits the same fields |
| D-15 | API-first; push is a stubbed adapter | No FCM work in v1 |
| D-16 | One subscriber (identified by email) → **one subscription** → **one location**, updatable | See D-17 for the consequence |
| D-17 | A location change of more than 1 km resets the alert state to `UNKNOWN` | Otherwise moving into existing rain produces a bogus "rain starting" mail |
| D-18 | Audience: private (me + friends); designed so going public later is a config/paperwork change, not a rewrite | Still: double opt-in, one-click unsubscribe, deletion endpoint |
| D-19 | Frontend: server-rendered HTML, no build step; Leaflet + OSM for the map | Non-technical friends must be able to subscribe |
| D-20 | Map picker page shows rain as an image overlay with a **time slider** | Added during the interview; drives the overlay renderer (§11) |
| D-21 | Radar decoding: **own minimal decoder** in the runtime; `wradlib` is a **test-only** dependency used as the golden reference | See §5 — answers the "wradlib or alternatives" question |
| D-22 | The slider spans **−12 h … +2 h**. Past frames are the **t+0 analysis frame of each past cycle**; future frames are leads 1…24 of the **latest** cycle | Still one DWD product (RV); the past is what the radar saw, not a re-forecast |
| D-23 | `evaluations` is a **rolling 48 h debug log** with a single TTL. The permanent per-subscriber record is `rain_events` + `notifications`, which are only written when something happens anyway | §8.1 — an indefinite row-per-subscriber-per-cycle series outgrows the entire national radar archive at ~13 500 subscribers, and ~99 % of it says "nothing happened" |
| D-24 | Production database is **Cloud SQL `db-f1-micro`, `europe-west3`**; dev and CI use Neon free or a local Postgres | §6.3 — Neon's free CU-hour allowance does not survive a 5-minute cadence, and it would add a second US processor for email + home coordinates |

---

## 3. Glossary

- **RV** — DWD RADVOR product: quantitative precipitation **nowcast**, 25 frames covering t+0 … t+120 min in 5-minute steps, published every 5 minutes. Successor to the retired `composit/fx`.
- **DE1200** — the national composite grid: **1200 rows × 1100 columns**, 1 km cells, WGS84 polar-stereographic projection, covering Germany plus border areas.
- **Cycle** — one RV publication, identified by its **nominal time** `T0` (UTC, always a multiple of 5 min).
- **Frame** — one of the 25 grids in a cycle; frame `k` is valid at `T0 + 5k` minutes, `k ∈ [0,24]`.
- **Lead time** — minutes into the future: `5k`.
- **Sample** — the aggregated value (max over the radius mask) of one frame for one subscription.

---

## 4. Data source: DWD Open Data

### 4.1 Product selection

The folders used in the user's earlier project no longer exist:

| Old | New | Notes |
|---|---|---|
| `/weather/radar/composit/rx/` (past reflectivity) | `/weather/radar/composite/wn/` | Reflectivity composite on DE1200 |
| `/weather/radar/composit/fx/` (`FX[0-9]+\.tar\.bz2`, nowcast reflectivity) | **`/weather/radar/composite/rv/`** | **This is what we use** |

`composite/` currently also contains `dmax`, `hg`, `hx`, `hymecng`, `pg`, `rs`, `vii`.

**Chosen product: `RV`.** Reasons:
1. It provides exactly the requested **5-minute forecast resolution**, 25 steps out to +120 min.
2. It is **quantitative precipitation** (mm per 5 min), not raw reflectivity — no dBZ→rain-rate
   conversion (Z-R relationship) needed, so the threshold in D-13 is directly meaningful.
3. It already includes the t+0 analysis frame, so "is it raining *now*?" (needed by D-2) comes from
   the same file. **We do not need a second product for the past.** The same t+0 frames, kept per
   cycle, also supply the map's 12 h history (§11.1). `RS` (past-hour totals) is not required for v1.

Base URL: `https://opendata.dwd.de/weather/radar/composite/rv/`
Files: `DE1200_RV<YYMMDDHHMM>.tar.bz2`, plus a rolling `DE1200_RV_LATEST.tar.bz2`.

> **VERIFIED 2026-09-16** against a real archive — see `docs/DWD_RV_FORMAT.md`, which is now the
> authoritative description of the format. 25 members, one per lead, each a raw RADOLAN binary of
> 195 header bytes + 1200×1100 little-endian `uint16`. Header carries dimensions, precision
> (`PR E-02` → 0.01 mm), interval, nominal time and the lead itself, so nothing is hard-coded and
> filename parsing is never required.

### 4.2 Licence and attribution (mandatory)

DWD open data is **CC BY 4.0** (since 2023; previously GeoNutzV). Commercial use permitted with
attribution. The service **must** display, on the map page, on the privacy page, and in the footer
of every alert email:

```
Datenbasis: Deutscher Wetterdienst (DWD), Radarprodukt RV, CC BY 4.0
```

Derived/processed values must be marked as modified ("eigene Verarbeitung von DWD-Daten").

### 4.3 Politeness policy toward opendata.dwd.de (hard requirements)

DWD publishes no explicit numeric rate limit, but the server is a shared public good and is known to
throttle abusive clients. The ingest worker **must**:

1. Perform **exactly one** product GET per 5-minute cycle in the happy path. Never per subscriber,
   never per request.
2. Send a descriptive `User-Agent`:
   `RainAlert/<version> (+https://<your-domain>; contact: <ops mailbox you control>)`.
   Do **not** put a personal/private address here.
3. Use conditional requests (`If-None-Match` / `If-Modified-Since`) when polling the `_LATEST` file;
   a `304` is the cheap, expected answer while waiting for a late cycle.
4. Never recursively crawl the directory tree. If listing is ever needed, use DWD's
   `content.log` / `content.log.gz` mechanism, which exists precisely for this.
5. Bound the wait for a late cycle: at most **5 attempts per cycle**, spaced with exponential backoff
   plus jitter (≈ 20 s, 40 s, 80 s, 160 s), then give up and let the next cycle handle it.
6. Honour `429` / `503` / `Retry-After` with backoff; after 5 consecutive failed cycles, open a
   circuit breaker (stop fetching for 15 min) and emit an operational alert.
7. Enforce byte budget guards — **per hour** (`DWD_HOURLY_BYTE_BUDGET`, default 512 MiB) as well as
   per day (`DWD_DAILY_BYTE_BUDGET`, default 8 GiB). Both halt ingestion rather than hammer DWD.
   Being a good citizen is a real requirement, but note which way these fail: **halting means nobody
   gets warned**, and the trigger is partly controlled by the other side. So the hourly budget makes
   exhaustion cost an hour rather than a day; a single capped response (§4.3.1) can never consume a
   meaningful fraction of either; and exhaustion **pages the operator immediately**, marks
   `/readyz` degraded, shows a banner in the UI, and is recorded in `radar_cycles.notes` so the gap
   in the timeline has an explanation. A silent day-long halt is not acceptable.
8. Be idempotent per cycle: a retried Cloud Run job execution must not re-download an
   already-archived cycle (unique key on `radar_cycles.nominal_time`).

### 4.3.1 The upstream archive is untrusted input (hard requirements)

DWD is a national weather service, not an adversary — but the *bytes* are untrusted all the same. A
compromised mirror, a hijacked route with a mis-issued certificate, a malicious proxy, and a plainly
corrupt publication are indistinguishable at the decoder, and because `_LATEST` re-serves the same
bytes every cycle, one bad file is an **indefinite** outage rather than a single failure.

1. **Cap the download.** Reject a response whose `Content-Length` exceeds 32 MiB, and stream with a
   hard byte counter that aborts at the same limit — `Content-Length` is attacker-supplied too.
2. **Cap the archive before reading it.** At most 32 members; every declared member size under
   8 MiB; declared total under 128 MiB. Reject the archive *as a whole* — decoding "the good parts"
   of a suspicious file lets the sender choose which forecast steps we see. Implemented in
   `decoder._check_limits`; a 483-byte archive declaring 512 MiB is a real, tested case.
3. **Read bounded.** Never `handle.read()` without a length; never `extractall()`; nothing is ever
   written to disk, so tar path traversal does not apply and must not start applying.
4. **Validate header fields against ranges, not just presence.** Reading dimensions and precision
   from the header (§5) is right, but unvalidated it means trusting a remote party with our
   allocation size and the scale of every reading: a forged `PR E+20` otherwise yields
   `precision = 1e+20`. Implemented in `decoder.parse_header`.
5. **Validate the nominal time against our own clock.** Reject a cycle stamped more than 15 minutes
   in the future or 3 hours in the past, and page rather than store — see §15, where this value is
   the key SLI. Cross-check it against the response's `Last-Modified`, which tracks it to within
   about 5 minutes (`DWD_RV_FORMAT.md` §4); a larger disagreement is an integrity signal.
6. **Plausibility-gate the decoded field** before any evaluation, recording the verdict in
   `radar_cycles.status`. Reject and page on: national max above 40 mm/5 min, a no-data fraction
   outside the observed 45–55 % band, or an implausible jump in wet fraction against the previous
   cycle. A gated cycle freezes state — it must never be evaluated as "dry" (§9 step 0).

### 4.4 Publication timing

RV for nominal time `T0` appears a few minutes after `T0`. **Measured** over a full 48 h listing
(see `docs/DWD_RV_FORMAT.md` §4): typically **+3 m 10 s … +3 m 30 s**, occasionally **+4 … +5 m**,
worst observed **+5 m 13 s**.

Therefore: Cloud Scheduler fires at **minute 4, 9, 14, … 59** (`cron: 4-59/5 * * * *`, UTC), and the
job then applies the bounded retry loop from 4.3 §5. Minute 4 puts the *first* attempt after the
common case, so the usual cycle costs exactly one request; the backoff (20/40/80/160 s, cumulative
300 s) still covers the five-minute tail. Firing at minute 3 — as this document specified before the
delay was measured — would land before publication on most cycles and burn a retry every time.

Note that a job started at minute 4 for `T0` is still fetching data for `T0`, not `T0−5`: the nominal
time comes from the file header (`_LATEST` carries no timestamp in its name), and `radar_cycles`
dedupes on it, so a cycle that slips past the next firing is simply picked up by that one.

Data staleness is a first-class monitored metric (§15) — a stale cycle must never be silently treated
as "no rain".

---

## 5. Radar decoding: wradlib or not? (answers the open question)

**`wradlib` is the correct reference implementation and the wrong runtime dependency for this service.**

| | wradlib at runtime | Own decoder + wradlib as test oracle (**chosen**, D-21) |
|---|---|---|
| Correctness | Battle-tested, maintained, handles all RADOLAN variants | Only handles RV; validated against wradlib in CI |
| Deps | numpy, xarray, xradar, optional GDAL/netCDF stack; image ≫ 1 GB | numpy + pyproj only; image ≈ 200 MB |
| Cold start | Slow imports hurt a 288×/day Cloud Run job | Fast |
| Format drift | Upstream fixes it for you | You own it — mitigated by the golden test |

**Decision:** implement `rainalert/radar/decoder.py`, roughly 150–250 lines:

1. Read the ASCII header up to the `ETX` (`0x03`) terminator; parse it into a dict of fields
   (product id, nominal datetime, dimensions `GP`, precision `PR`, interval `INT`, forecast minutes
   `VV`, format version `VS`, and the secondary-data / flag definitions).
2. Read the payload as `numpy.uint16` little-endian, reshape to `(rows, cols)` taken **from the
   header**, not from constants.
3. Apply the **precision factor from the header's `PR` field** (e.g. `E-02` → ×0.01) to convert raw
   counts to millimetres per 5-minute interval. Do not hard-code the exponent.
4. Identify no-data by **comparing against the sentinel `0x29C4`**, before any bit masking. Missing is
   **not** zero and **must not** be interpreted as "dry" (§9, step 0).
   **Never strip flag bits blind:** `0x29C4 & 0x0FFF = 2500`, which after the precision factor is a
   plausible-looking 25.00 mm/5 min — so the naive decode silently turns 47 % of the grid into
   extreme rain and alerts everyone, forever. Required test case (§16.1).
   Equally: **do not read data quality out of the header's `MS` radar-site list.** It lags reality — a
   site absent from `MS` can still be contributing, and vice versa (`DWD_RV_FORMAT.md` §5). Coverage
   comes from the no-data mask and nothing else.
5. Return `(values: float32 [rows, cols], missing: bool [rows, cols], header: dict)`.

Georeferencing (`rainalert/radar/grid.py`):
- Build the DE1200 → WGS84 transform once with `pyproj` from the projection parameters, and the
  inverse `lat/lon → (row, col)` mapping.
- **Pin it with a test** that compares against `wradlib.georef.get_radolan_grid(1200, 1100, wgs84=True)`
  at a set of reference points (corners, centre, a handful of German cities) with a tolerance of one
  half cell. This is the single most likely place to introduce a silent, systematic bug — an offset
  of a few cells means warning the wrong village.
- Cache the derived `(row, col)` and the radius mask per subscription; invalidate on location update.

`wradlib` lives in the `dev`/`test` dependency group only and is never imported by runtime code.
CI enforces this with an import-linter rule.

---

## 6. Architecture

```
Cloud Scheduler (cron 4-59/5 * * * *, UTC)
        │ OIDC
        ▼
Cloud Run Job: ingest ────────────────────────────────────────────┐
  1. conditional GET DE1200_RV_LATEST.tar.bz2  (≤5 tries, backoff) │
  2. dedupe by nominal_time (unique) ─────────────► Postgres       │
  3. archive raw .tar.bz2 ───────────────────────► GCS  (48 h TTL) │
  4. decode 25 frames (numpy, in memory)                           │
  5. sample per distinct radius mask (deduped) ► fan out to subs    │
  6. evaluate state machine ► enqueue alerts ─────► Postgres       │
  7. render overlays: 1 observed + 24 forecast ──► GCS (14 h / 1 h)│
  8. deliver queued alerts via Notifier ─────────► mail provider   │
└──────────────────────────────────────────────────────────────────┘

Cloud Run Service: api  (scale to zero, min-instances 0)
  FastAPI + Jinja2:  /  /confirm  /manage  /unsubscribe  /privacy
                     /api/v1/... , /healthz, /readyz, /metrics
        │                    │
        ▼                    ▼
   Postgres            GCS (overlay PNGs, public-read or proxied)
```

**Why steps 5–8 live in the same job as 4:** the decoded grids are needed by both the sampler and the
renderer. Measured, a complete 25-frame cycle as `float32` plus boolean masks is **165 MB** (the
66 MB figure an earlier revision used is the raw `uint16` size), before the renderer's reprojection
buffers — size the task from 165 MB, not from 66. Splitting would mean re-reading from GCS for no benefit at this
scale. If subscriber count ever makes step 6 slow, split 7 (rendering) into its own job first — it is
the only part that is not on the alerting critical path.

**Delivery ordering:** alerts are written to `notifications` with `status='queued'` inside the same
transaction as the state transition, then delivered. A crash after commit but before send is
recovered by the next cycle, which re-attempts `queued` rows older than 60 s and marks anything
older than 30 min `expired` (a late rain warning is worse than none).

### 6.1 GCP resource list

| Resource | Purpose | Notes |
|---|---|---|
| Cloud Run **job** `rainalert-ingest` | the 5-minute pipeline | `--max-retries 1`, `--task-timeout 240s`, **concurrency 1** |
| Cloud Run **service** `rainalert-api` | API + web UI | min instances 0, **`--max-instances` set explicitly** (see below) |
| Cloud Scheduler `rainalert-tick` | `4-59/5 * * * *` UTC | invokes the job via OIDC SA; minute 4 is measured, not guessed (§4.4) |
| GCS bucket `rainalert-data` | `raw/` (48 h), `overlays/obs/` (14 h), `overlays/fc/` (1 h) | per-prefix lifecycle rules, uniform ACL |
| Secret Manager | DB URL, mail API key, `SECRET_KEY` | mounted as env |
| Artifact Registry | one container image, two entrypoints | |
| Cloud Logging / Monitoring | structured logs, staleness alert | §15 |
| Postgres | **Cloud SQL `db-f1-micro`, `europe-west3`** | see §6.3 for why, not Neon free |

Cloud Run job concurrency must be 1 (plus a Postgres advisory lock `pg_try_advisory_lock` around the
pipeline) so a retried execution can never double-send.

`--max-instances` on the API service is a **security control, not a tuning knob**. Left at the
platform default, unauthenticated traffic to any endpoint that touches the database — `/readyz` does
so by definition — scales out until the database's connection ceiling is exhausted, which **starves
the ingest job of the connection it needs to alert anyone**, and bills us for the privilege.
Scale-to-zero means traffic costs money, so cost amplification is a real attack here rather than a
theoretical one. Set it low (single digits at this scale), give the ingest job its own database role
with reserved connections, and never let the web tier be able to take alerting down.

### 6.2 Cost estimate (rough, EUR/month, single-digit subscriber count)

| Item | Calculation | ≈ |
|---|---|---|
| Ingest compute | 288 runs/day × ~30 s × 1 vCPU / 2 GiB ≈ 72 vCPU-h/month, partly free-tier | 1–3 |
| API service | scale-to-zero, a few requests/day | ~0 |
| GCS raw archives | ~5 MB/cycle × 288/day ≈ 1.4 GB/day, 48 h retention ≈ 3 GB | <0.1 |
| GCS overlays | observed: 144 × ~150 KB rolling ≈ 22 MB; forecast: 24/cycle × 1 h ≈ 43 MB | <0.1 |
| Egress | overlay PNGs to a handful of browsers | ~0 |
| Mail | free tier (~300/day) | 0 |
| Database | Cloud SQL `db-f1-micro` + storage + backups | ~9–12 |
| **Total** | | **~11–17** — the database is most of the bill (§6.3) |

Numbers are estimates; the archive size depends strongly on how much it is raining (bz2 of a dry
grid is tiny). The daily byte budget guard (4.3 §7) is the backstop.

---

### 6.3 Why Cloud SQL and not Neon's free tier

Both are managed Postgres and the code is DSN-agnostic, so this is not a technical lock-in. Two
things decided it, and both are specific to *this* workload rather than general advice.

**The 5-minute cadence defeats scale-to-zero billing.** Neon's free plan allows 100 CU-hours per
month and suspends compute after an idle period, 300 s by default. The ingest job queries every
300 s, so the idle timer keeps resetting and the compute plausibly never suspends. At the 0.25 CU
floor that is roughly 730 h × 0.25 = **180 CU-hours against a 100 CU-hour allowance — exhausted
around day 16**, after which compute stays suspended until the month rolls over. That is a silent
two-week outage every month, in a service whose entire failure mode is "nobody gets warned and the
dashboard looks fine".

It can be tuned to fit — dropping autosuspend to ~60 s costs about 65 s of wakefulness per cycle,
roughly 39 CU-hours/month — but that is production running on a free tier trimmed to fit, with
arithmetic to re-verify whenever the cadence or the plan changes. Not worth €10/month.

**It would add a second processor for the most sensitive table.** We store email addresses and
precise home coordinates of private individuals in Germany. Cloud SQL in `europe-west3` keeps that
inside Google, which is already the processor for Cloud Run and GCS: one DPA, one subprocessor
entry. Neon is a second processor, US-headquartered since the Databricks acquisition, so the CLOUD
Act question exists even with data in an EU region — and §13 is built on data minimisation.

Cutting the other way: `db-f1-micro` defaults to about 25 connections, which makes the connection
starvation in SECURITY_REVIEW.md F-6 *sharper* than it would be behind Neon's pooler. That is an
argument for setting `--max-instances` low and giving the ingest job its own role with reserved
connections — both of which F-6 already requires — not for changing database. Note also that
shared-core instances carry no SLA; at this scale that is acceptable, and it is the reason to keep
the DSN-agnostic code rather than adopt Cloud SQL-specific features.

**Where Neon is the better answer:** outside GCP, for genuinely bursty workloads that are idle most
of the time, or when branch-per-PR databases are worth having. Hence the split actually adopted —
**Neon free (or a local Postgres, which is what the test suite uses) for development and CI; Cloud
SQL for production.** That costs nothing extra and puts Neon's branching where it is useful.

---

## 7. Data model (PostgreSQL)

```sql
CREATE TYPE subscription_status AS ENUM ('pending','active','paused','deleted');
CREATE TYPE alert_state       AS ENUM ('UNKNOWN','DRY','WARNED','RAINING');
CREATE TYPE cycle_status      AS ENUM ('ok','partial','failed');
CREATE TYPE token_purpose     AS ENUM ('confirm','manage','api','unsubscribe');

CREATE TABLE subscribers (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL,                -- stored lowercased; CITEXT if available
  email_hash    bytea NOT NULL,               -- sha256(lower(email)) for lookups/rate limiting
  locale        text NOT NULL DEFAULT 'de',
  created_at    timestamptz NOT NULL DEFAULT now(),
  confirmed_at  timestamptz,
  deleted_at    timestamptz,
  UNIQUE (email_hash)
);

-- v1: exactly one row per subscriber (enforced by a partial unique index).
-- The separate table keeps 1:N possible later without a migration of the alerting code.
CREATE TABLE subscriptions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subscriber_id          uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  status                 subscription_status NOT NULL DEFAULT 'pending',
  lat                    double precision NOT NULL,
  lon                    double precision NOT NULL,
  location_updated_at    timestamptz NOT NULL DEFAULT now(),
  grid_row               integer,             -- cached projection of (lat,lon)
  grid_col               integer,
  -- alert rule (D-14: per-subscription, defaults from D-13/D-3)
  radius_m               integer NOT NULL DEFAULT 2000  CHECK (radius_m BETWEEN 0 AND 20000),
  threshold_mm_5min      numeric(5,2) NOT NULL DEFAULT 0.10 CHECK (threshold_mm_5min > 0),
  lead_time_minutes      integer NOT NULL DEFAULT 30 CHECK (lead_time_minutes BETWEEN 5 AND 120),
  -- throttling (D-9/D-10: disabled by default)
  min_gap_minutes        integer NOT NULL DEFAULT 0 CHECK (min_gap_minutes >= 0),
  quiet_hours_start      time,                -- NULL = disabled
  quiet_hours_end        time,
  timezone               text NOT NULL DEFAULT 'Europe/Berlin',
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_subscription_per_subscriber
  ON subscriptions (subscriber_id) WHERE status <> 'deleted';
CREATE INDEX active_subscriptions ON subscriptions (status) WHERE status = 'active';

CREATE TABLE auth_tokens (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subscriber_id uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  purpose       token_purpose NOT NULL,
  token_hash    bytea NOT NULL UNIQUE,        -- sha256 of a 32-byte random token; plaintext never stored
  expires_at    timestamptz,                  -- NULL for long-lived api tokens
  used_at       timestamptz,                  -- single-use for 'confirm'
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz
);

CREATE TABLE radar_cycles (
  id            bigserial PRIMARY KEY,
  nominal_time  timestamptz NOT NULL UNIQUE,  -- always a 5-minute boundary, UTC
  fetched_at    timestamptz NOT NULL DEFAULT now(),
  source_url    text NOT NULL,
  etag          text,
  sha256        bytea NOT NULL,
  bytes         integer NOT NULL,
  frame_count   integer NOT NULL,
  status        cycle_status NOT NULL DEFAULT 'ok',
  archive_uri   text,                         -- gs://.../raw/DE1200_RV<...>.tar.bz2
  obs_overlay_uri    text,                    -- gs://.../overlays/obs/<nominal_time>.png
  fc_overlay_prefix  text,                    -- gs://.../overlays/fc/<nominal_time>/
  notes         text
);

CREATE TABLE evaluations (
  id                     bigserial PRIMARY KEY,
  subscription_id        uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  cycle_id               bigint NOT NULL REFERENCES radar_cycles(id) ON DELETE CASCADE,
  evaluated_at           timestamptz NOT NULL DEFAULT now(),
  now_wet                boolean NOT NULL,
  first_hit_lead_minutes integer,             -- NULL = no hit inside the lead window
  max_rate_by_lead       real[] NOT NULL,     -- 25 slots, mm per 5 min, index = lead/5 (NOT
                                              -- the frame's position: see 8.2), NULL where absent
  missing_fraction       real[] NOT NULL,     -- same indexing as max_rate_by_lead
  state_before           alert_state NOT NULL,
  state_after            alert_state NOT NULL,
  decision               text NOT NULL,       -- alert | no_rain | suppressed_state |
                                              -- suppressed_gap | suppressed_quiet | skipped_missing
  UNIQUE (subscription_id, cycle_id)
);
CREATE INDEX evaluations_by_sub_time ON evaluations (subscription_id, evaluated_at DESC);
-- D-23: the whole table is purged past EVALUATION_RETENTION_HOURS (48 h, matching the raw archives,
-- so any incident inside that window is fully reconstructable). Nothing here is permanent; the
-- durable per-subscriber record is rain_events + notifications below.

CREATE TABLE alert_states (
  subscription_id uuid PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
  state           alert_state NOT NULL DEFAULT 'UNKNOWN',
  state_since     timestamptz NOT NULL DEFAULT now(),
  dry_since       timestamptz,
  current_event_id bigint,
  last_alert_at   timestamptz
);

CREATE TABLE rain_events (
  id                 bigserial PRIMARY KEY,
  subscription_id    uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  predicted_start_at timestamptz NOT NULL,
  first_alert_at     timestamptz,
  observed_start_at  timestamptz,             -- when t+0 first exceeded the threshold
  ended_at           timestamptz,
  peak_mm_5min       real,
  verified           boolean                  -- observed within ±15 min of prediction
);

CREATE TABLE notifications (
  id             bigserial PRIMARY KEY,
  subscription_id uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  event_id       bigint REFERENCES rain_events(id) ON DELETE SET NULL,
  channel        text NOT NULL DEFAULT 'email',
  status         text NOT NULL DEFAULT 'queued',   -- queued|sent|failed|expired
  queued_at      timestamptz NOT NULL DEFAULT now(),
  sent_at        timestamptz,
  provider_message_id text,
  error          text,
  payload        jsonb NOT NULL
);
CREATE INDEX notifications_pending ON notifications (status, queued_at) WHERE status = 'queued';
```

Conventions: all timestamps `timestamptz`, stored UTC; presentation converts to
`subscriptions.timezone`. Migrations with Alembic.

---

## 8. Sampling

For each active subscription, per cycle:

1. `(row, col)` from the cached projection; recompute if `grid_row IS NULL`.
2. Radius mask: all cells whose centre is within `radius_m` of the subscriber point. At 1 km
   resolution and the default 2000 m this is ≈ 13 cells. Computed once and cached in memory, keyed by
   `(grid_row, grid_col, radius_m)` — many subscribers in the same town share a mask.
3. For every frame `k ∈ [0,24]`: `max_rate_by_lead[k] = max(values[mask] where not missing)`, and
   `missing_fraction[k] = mean(missing[mask])` — **per frame**. The no-data region advects with the
   forecast (measured: 102 615 cells go valid → no-data between t+0 and t+120, and 79 483 the other
   way; `DWD_RV_FORMAT.md` §9), so a point near the edge of coverage can have good data now and none
   at t+45. Taking frame 0's mask for all frames would evaluate garbage for exactly those users.
4. **The loop is fault-isolating.** Each subscription is evaluated inside its own try/except: a
   failure records `decision='error'`, increments a metric, and the loop continues. Without this, one
   unevaluatable row — a `NaN` latitude that arrived through the API, an unknown `timezone` string —
   aborts the run for *everyone*, every cycle, permanently, and §13's ban on logging exact
   coordinates makes it hard to find which row is at fault. A subscription that keeps failing is
   surfaced as unhealthy in `/subscriptions/me` and `/manage`, so the user is told rather than
   silently never warned.
5. Coverage is **not** the same as being inside the grid: the DE1200 rectangle is much larger than the
   radar network's reach, and ~47 % of it is no-data even in perfect conditions. A subscription is
   flagged `out_of_coverage` when its mask is entirely no-data at t+0 across several consecutive
   cycles — not merely when the point falls outside the grid. The UI/API says so explicitly rather
   than silently never alerting.

Rationale for `max` rather than `mean`: a 2 km radius around a point, one of whose cells is under a
shower, means the user gets wet. Mean would dilute small convective cells, which is exactly the case
this service exists for.

### 8.0 Index by lead, never by position

`max_rate_by_lead` and `missing_fraction` are indexed by **`lead_minutes / 5`**, built from a
`dict[int, float]` keyed on the frame's own `lead_minutes`. They are *not* the decoded frame list
indexed by position.

The two agree only when all 25 members are present with leads exactly 0, 5, … 120. If a cycle
arrives with `_015` missing, positional indexing shifts every later entry down one slot: rain at +60
is emailed as rain at +55, and the wrong `predicted_start_at` is written to the permanent
`rain_events` record — which then corrupts the verification job that exists to tune the thresholds.
With a forged `VV` field the misalignment is chosen by the sender. Absent leads stay NULL, which the
§9 per-frame gate already treats as "excluded, not dry".

The ingest path asserts completeness — leads exactly `range(0, 125, 5)`, one `nominal_time` — and
marks anything else `status='partial'`. (§16.1 tells the *fixture* tests not to assert a member
count, because fixtures hold three frames; that instruction must not leak into the pipeline.)

### 8.1 Why sample per subscription at all?

A fair objection: the radar field is one global object of fixed size, while per-subscriber data grows
with the number of subscribers. Past some N, storing anything per subscriber per cycle costs more
than storing the whole national grid — which contains strictly more information anyway.

That is true, and the crossover is low. One `evaluations` row is ≈ 300 bytes with its indexes; one
cycle's compressed RV archive is ≈ 5 MB. They cost the same at ≈ **13 500 subscribers** — and the
archive is a fixed cost per cycle at *any* N, while the rows multiply.

The flaw was not that the data is per subscriber. It is that it is **cycle-shaped**: 288 rows per
subscriber per day, ~99 % of which record "dry, nothing happened". Hence D-23 — `evaluations` is a
rolling 48 h debug log with one TTL and no exceptions, and the durable per-subscriber record is
`rain_events` + `notifications`, which are only written when something actually happens and so need no
retention machinery of their own.

Accepted limitation: once an event ages past 48 h, we know *that* we warned and when, but not the
exact rule values in force at the time. Recording those would mean another column and another code
path to answer a question a handful of users will ask about twice a year. Not worth it (see the
guiding principle in §1).

The sampling itself is deduplicated by mask key `(grid_row, grid_col, radius_m)`, so its cost is
bounded by the number of *distinct* masks rather than the subscriber count. That is a one-line
dictionary lookup, not an optimization project, and it is where the story should stop until a
measurement says otherwise.

---

## 9. Alert evaluation and state machine

Per subscription, per cycle, with `threshold = threshold_mm_5min`, `L = lead_time_minutes`:

```
now_wet   := max_rate_by_lead[0] >= threshold
hits      := { k : 1 <= k <= L/5 and max_rate_by_lead[k] >= threshold }
first_hit := min(hits) or None
```

**Step 0 — data quality gate.** If `missing_fraction[0] > 0.30`, record `decision='skipped_missing'`,
leave the state unchanged, and do nothing else. Radar outage must never be read as "dry" and must
never clear a `WARNED` state.

Frames beyond t+0 are gated individually: a frame whose `missing_fraction[k]` exceeds the limit is
excluded from `hits` rather than counted as dry, so losing coverage at long lead delays a warning
instead of suppressing one.

**States and transitions**

| From | Condition | To | Mail? |
|---|---|---|---|
| `UNKNOWN` | `now_wet` | `RAINING` | no — rain is already falling and we cannot tell for how long, so there is nothing useful to say |
| `UNKNOWN` | not `now_wet` and `first_hit` exists | `WARNED` | **yes** — dry here, rain approaching: we already know enough |
| `UNKNOWN` | not `now_wet` | `DRY` | no |
| `DRY` | `first_hit` exists and not `now_wet` | `WARNED` | **yes** (unless suppressed) |
| `DRY` | `now_wet` (rain started without a prior warning, e.g. formed overhead) | `RAINING` | no |
| `DRY` | otherwise | `DRY` | no |
| `WARNED` | `now_wet` | `RAINING` | no — the warned-about rain arrived |
| `WARNED` | no hit for 3 consecutive cycles (15 min) | `DRY` | no — forecast retracted |
| `WARNED` | otherwise | `WARNED` | no |
| `RAINING` | dry at t+0 continuously for `dry_clear_minutes` (default 30) | `DRY` | no |
| any | location moved > 1 km (D-17) | `UNKNOWN` | no |

On the `DRY → WARNED` transition: open a `rain_events` row with
`predicted_start_at = T0 + 5·first_hit`, queue the notification, set `last_alert_at`.

**Blast-radius limit (global, per cycle).** Before any mail is queued, count the transitions this
cycle would produce. If more than `min(50 % of active subscriptions, BLAST_RADIUS_MAX)` subscriptions
would be warned at once, queue **nothing**, record the cycle as `partial`, and send a single operator
alert instead. A national squall line is real and will trip this; at this scale a human confirming it
once is cheap, and it is the only control that bounds the worst case — a poisoned or corrupt cycle
that reads as rain everywhere would otherwise mail the entire list in one go *and* burn the mail
provider's daily quota, so that the day's genuine alerts are never delivered. There is also a
per-run absolute ceiling on mails, with confirmation mail drawing from a separate reserve so it
cannot starve alert mail.

**Suppression checks** (evaluated in this order, before queuing):
1. `min_gap_minutes > 0` and `now - last_alert_at < min_gap_minutes` → `suppressed_gap`
   (state still advances to `WARNED`, so no duplicate fires later).
2. quiet hours configured and local time inside the window → `suppressed_quiet` (dropped, not queued
   for later — a warning delivered at 06:00 about rain at 03:00 is noise).

Both are **disabled by default** per D-9. The code path exists and is unit-tested so enabling them
later is a config change.

**Why this shape:** a fixed cooldown either spams during showers or misses the second front. Tying
suppression to the physical event (it must go dry again for 30 minutes) gives exactly one mail per
rain event, which is what "not too many notifications" means in practice.

**Verification loop (cheap, high value):** `rain_events` rows are kept indefinitely (D-23), so a daily
job can compare `rain_events.predicted_start_at` with the later-observed t+0 frames and populate
`verified`, giving hit rate and false-alarm ratio. It runs inside the 48 h window, while the archived
grids are still there. Build this in M4; it is the only honest way to
tune the defaults in D-13 later.

---

## 10. API

Base path `/api/v1`. JSON in/out. Auth: `Authorization: Bearer <token>` with an `api` token.
All endpoints return RFC 7807 problem details on error.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/subscriptions` | none (rate limited) | `{email, lat, lon, radius_m?, threshold_mm_5min?, lead_time_minutes?}` → `202`. Always responds identically whether or not the address is already known (no account enumeration). Sends the confirmation mail. |
| `GET` | `/confirm?token=…` | confirm token | Activates the subscription, issues the long-lived `api` token, renders it once on the manage page. Single use, 24 h expiry. |
| `GET` | `/subscriptions/me` | api | Current location + rule + state + last evaluation. |
| `PUT` | `/subscriptions/me/location` | api | `{lat, lon}` → `204`. The endpoint the future mobile app calls. Applies D-17. Rate limited to 1 per 60 s. |
| `PATCH` | `/subscriptions/me` | api | Update rule fields (`radius_m`, `threshold_mm_5min`, `lead_time_minutes`, `min_gap_minutes`, quiet hours, `timezone`). Present in v1 API, not yet exposed in the v1 UI. |
| `POST` | `/subscriptions/me/pause` / `/resume` | api | Temporarily stop alerts without deleting data. |
| `DELETE` | `/subscriptions/me` | api | Hard-deletes subscriber, subscription, tokens, evaluations, notifications. Returns `204`. |
| `GET` | `/forecast?lat=&lon=&radius_m=` | api | The 25 sampled values for an arbitrary point + a human summary (`"rain starting in ~20 min, light"`). Powers the app and manual testing. |
| `GET` | `/overlays/timeline?past_hours=` | none | The full slider manifest, default `past_hours=12`: `{now, latest_cycle, bounds:[[s,w],[n,e]], width, height, colorscale:[…], attribution, gaps:[…], frames:[{offset_minutes, valid_time, kind:"observed"｜"forecast", source_cycle, url}]}`. `offset_minutes` is negative for the past, ordered ascending. Cache-Control 60 s. |
| `GET` | `/unsubscribe?token=…` | unsubscribe token | One-click unsubscribe (also accepts `POST` for RFC 8058 `List-Unsubscribe-Post`). |
| `GET` | `/healthz`, `/readyz` | none | Liveness / readiness (readiness = DB reachable). |
| `GET` | `/metrics` | internal | Prometheus-format metrics (§15). |

Rate limits (per IP and per email hash): `POST /subscriptions` 5/hour, `PUT location` 60/hour,
`GET /forecast` 120/hour. Implemented in-process with a Postgres-backed counter; good enough at this
scale, replaceable later.

---

## 11. Web UI

Server-rendered Jinja2, no build step, no SPA. Pages:

- **`/` — subscribe.** Leaflet map (OSM tiles), "use my location" button (browser geolocation),
  draggable marker, email field, consent checkbox with a one-line purpose statement, submit.
  **Plus the rain timeline overlay + slider (D-20, D-22).**
- **`/confirm`** — result page; shows the API token once with a copy button ("you will need this for
  the app later"), and the manage link.
- **`/manage`** — current location on the map, rule values (read-only in v1), pause/resume, delete.
- **`/unsubscribe`** — confirmation of one-click unsubscribe.
- **`/privacy`**, **`/attribution`** — §4.2 and §13.

### 11.1 Rain timeline overlay (−12 h … +2 h)

Two kinds of frame, both produced from RV — no second DWD product:

- **Observed** (`offset_minutes <= 0`): the **t+0 analysis frame of each past cycle**, one per cycle,
  5-minute spacing — 144 frames over 12 h. This is what the radar actually measured.
- **Forecast** (`offset_minutes > 0`): leads 1…24 of the **latest** cycle only, +5 … +120 min.

The asymmetry matters and must be visible in the UI: past frames each come from their own cycle,
while all future frames come from one. A user must never mistake a forecast frame for an
observation, so the two are labelled differently and the boundary at *now* is marked on the slider.

**Renderer** (ingest step 7), once per cycle:

1. Reproject the frame from DE1200 polar-stereographic to **EPSG:3857** into a fixed axis-aligned
   bounding box covering Germany, nearest neighbour, using `pyproj` directly. **Row 0 of the grid is
   the southern edge**, so the array is flipped vertically for a north-up image. Compute the mapping in
   the straightforward way each run; if it ever shows up in `rainalert_pipeline_seconds`, cache it
   then.
   *Why a Mercator box:* Leaflet's `L.imageOverlay` only places axis-aligned, unrotated images by
   lat/lng bounds. Overlaying the native grid directly would be visibly skewed.
2. Map values to RGBA with a documented colour scale (transparent below the light-rain threshold, then
   a perceptually ordered ramp). Encode as palettised PNG at half resolution (≈ 550×600) — ~100–200 KB.
3. Upload, with different lifetimes because the two kinds are consumed differently:
   - analysis → `gs://…/overlays/obs/<nominal_time>.png` — kept **14 h** (12 h window + margin)
   - forecasts → `gs://…/overlays/fc/<nominal_time>/rv_<lead_minutes:03d>.png` — kept **1 h**, since
     only the newest cycle's forecast is ever served

**Manifest.** `GET /api/v1/overlays/timeline?past_hours=12` is a query over `radar_cycles` for the
window, returning frames ordered by `offset_minutes` ascending.

**Gaps.** A cycle the ingester never obtained is a hole in the timeline. The manifest lists missing
intervals explicitly and the client renders a gap — no overlay, a "no data" label — rather than
holding the previous image, which would fake continuity across a radar outage.

**Client.**

- Slider spans `−past_hours·60 … +120` in 5-minute steps (168 positions by default), starts at `0`
  (now), with a visible *now* tick separating observed from forecast.
- **Do not preload everything.** 168 PNGs is ≈ 25 MB — fine on a desktop, hostile on a phone with
  mobile data. Load the frame under the cursor, keep a window of ±6 frames prefetched, prefetch ahead
  in the direction of travel, and LRU-evict beyond ~40 images.
- Play button sweeps −60 min → +120 min at ~8 fps and loops; dragging the slider cancels playback.
- Labels show the absolute **local** time, the relative offset (`−45 min` / `+35 min`), the source
  cycle's nominal time, and `observed` / `forecast`.
- If the latest cycle is older than 20 minutes, the page shows a clear "radar data is stale" banner
  instead of pretending.

**Backfill.** A fresh deployment has no history, so the timeline starts empty and fills at one frame
per 5 minutes. `rainalert backfill --hours 12` fetches past cycles **sequentially** (one request at a
time, ≥ 2 s apart, byte budget and all other §4.3 rules honoured) and renders their analysis
overlays. A separate re-render path rebuilds observed overlays from the 48 h raw archives without
touching DWD at all — prefer it whenever the archive still has the cycle.
*How far back the `rv/` directory actually keeps files is an **M0 VERIFY** item*; if DWD only retains
a few hours, backfill can fill only that much and the rest accrues over time.

**OSM tile policy:** the OpenStreetMap public tile servers are a donated resource with a usage policy
that forbids heavy use. Fine for a private map picker with correct `User-Agent`/referrer; before any
public launch, switch to a proper tile provider (see **Q-5**).

---

## 12. Notifications

```python
class Notifier(Protocol):
    def send(self, message: OutboundMessage) -> DeliveryResult: ...
```

Adapters: `console` (dev, prints), `smtp` (generic), `brevo`/`mailgun`/`sendgrid` (HTTP API, default
in prod), `push` (**stub**, raises `NotImplementedError` — D-15). Selected by `NOTIFIER` env var.

Alert mail:
- Subject: `Regen in ~20 Minuten (Musterstadt)` — lead time and a place name resolved once at
  subscribe time (reverse geocode is optional; falls back to coordinates).
- Body (text + minimal HTML): predicted start time (local), expected intensity class, the 30-minute
  series in a compact line, the map link, the data timestamp, the DWD attribution, and the
  unsubscribe link.
- Headers: `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058),
  `Auto-Submitted: auto-generated`.
- Deliverability: SPF + DKIM + DMARC on the sending domain are a **hard prerequisite** for M6; without
  them these mails land in spam and the whole service is pointless.

Transactional mails: confirmation (double opt-in), deletion confirmation. No marketing mail, ever.

---

## 13. Security, privacy, GDPR

The service processes **email address + precise location** of identifiable people in Germany. Even as
a private project this is personal data under GDPR.

- **Legal basis:** consent (Art. 6(1)(a)), obtained via double opt-in; the confirmation timestamp and
  source IP hash are the consent record.
- **Data minimisation:** store only email, coordinates, rule settings, and the alerts actually sent.
  No location history (the location is overwritten in place — D-16), no IP logs beyond a hashed value
  for rate limiting, retained 7 days. The per-cycle `evaluations` log is purged after 48 h (D-23): an
  indefinite 5-minute-resolution series per subscriber would be a presence log, which is more personal
  data than this service has any reason to hold.
- **Purpose limitation:** the stored evaluation rows contain coordinates-derived samples only, never
  raw identifying data beyond the subscription id.
- **Deletion:** `DELETE /api/v1/subscriptions/me` and the unsubscribe link both hard-delete
  (cascade). Unconfirmed subscriptions are purged after 24 h; subscriptions with no location update
  and no login for 12 months are purged after a warning mail.
- **Transport:** HTTPS only, HSTS, secure cookies (`SameSite=Lax`), CSP without inline scripts except
  a nonce for the map bootstrap.
- **Tokens:** 32 bytes from `secrets.token_urlsafe`, stored only as SHA-256; confirm tokens single-use
  with 24 h expiry; api tokens revocable; constant-time comparison.
- **Abuse:** subscribe endpoint is rate limited and only ever sends mail to an address after the
  double opt-in, so it cannot be used as a mail relay.
- **Secrets** in Secret Manager, never in the repo; `.env.example` holds names only.
- **Logging:** never log full email addresses or exact coordinates; log `subscriber_id` and
  coordinates rounded to 2 decimals (~1 km) instead.

**Deferred until a public launch (D-18):** Impressum (§5 TMG/DDG), full Datenschutzerklärung,
Auftragsverarbeitungsvertrag with the mail provider, a cookie/consent banner if third-party tiles are
used. Tracked as **Q-2**.

---

## 14. Configuration

All configuration via environment variables, parsed by a single pydantic `Settings` object.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | — | Postgres DSN |
| `GCS_BUCKET` | — | archives + overlays |
| `DWD_BASE_URL` | `https://opendata.dwd.de/weather/radar/composite/rv/` | product directory |
| `DWD_USER_AGENT` | — | must include contact URL/mailbox (§4.3) |
| `DWD_MAX_ATTEMPTS` | `5` | per cycle |
| `DWD_DAILY_BYTE_BUDGET` | `8589934592` | 8 GiB guard; halting pages the operator (§4.3 rule 7) |
| `DWD_HOURLY_BYTE_BUDGET` | `536870912` | 512 MiB guard, so exhaustion costs an hour not a day |
| `DWD_MAX_RESPONSE_BYTES` | `33554432` | 32 MiB hard cap on a single response (§4.3.1) |
| `CYCLE_MAX_FUTURE_MINUTES` | `15` | reject a cycle stamped further ahead than this |
| `CYCLE_MAX_AGE_HOURS` | `3` | reject a cycle stamped further back than this |
| `PLAUSIBILITY_MAX_MM_5MIN` | `40` | national max above this gates the cycle |
| `BLAST_RADIUS_MAX` | `25` | absolute cap on subscriptions warned in one cycle (§9) |
| `RAW_RETENTION_HOURS` | `48` | GCS lifecycle (D-7) |
| `EVALUATION_RETENTION_HOURS` | `48` | purge of the `evaluations` debug log (D-23) |
| `OVERLAY_OBS_RETENTION_HOURS` | `14` | GCS lifecycle, observed frames (12 h window + margin) |
| `OVERLAY_FC_RETENTION_HOURS` | `1` | GCS lifecycle, forecast frames |
| `TIMELINE_PAST_HOURS` | `12` | default/maximum past span of the slider (D-22) |
| `DEFAULT_RADIUS_M` | `2000` | D-3 |
| `DEFAULT_THRESHOLD_MM_5MIN` | `0.1` | D-13 |
| `DEFAULT_LEAD_MINUTES` | `30` | D-13 |
| `DEFAULT_MIN_GAP_MINUTES` | `0` | D-9/D-10 — off |
| `DRY_CLEAR_MINUTES` | `30` | `RAINING → DRY` |
| `WARNED_RETRACT_CYCLES` | `3` | `WARNED → DRY` |
| `MISSING_FRACTION_LIMIT` | `0.30` | data quality gate |
| `NOTIFIER` | `console` | `console｜smtp｜brevo｜mailgun｜sendgrid｜push` |
| `MAIL_FROM`, `MAIL_API_KEY` | — | provider credentials |
| `PUBLIC_BASE_URL` | — | link generation |
| `SECRET_KEY` | — | token/HMAC signing |
| `LOG_LEVEL` | `INFO` | structured JSON logs |

---

## 15. Observability

Metrics (Prometheus text on `/metrics`, mirrored to Cloud Monitoring):
- `rainalert_cycle_age_seconds` — **the key SLI**: now − latest `radar_cycles.nominal_time`.
  Alert if > 20 min. Note this value derives from a field **the remote party supplies**, so it is
  clamped at zero and a **negative raw age is its own paging condition**: a cycle stamped in the
  future would otherwise read as "the freshest data we ever had" and silence this alert until real
  time caught up. §4.3.1 rule 5 rejects such a cycle at ingest; this alert is the backstop for
  whatever slips through.
- `rainalert_cycle_status_total{status=ok|partial|rejected}` — a cycle can be fetched, stored and
  *meaningless*. Staleness monitoring does not cover "fetched, parsed, and implausible", which is
  exactly the shape of a poisoned-upstream attack that suppresses everyone's alerts while the
  dashboard stays green.
- `rainalert_fetch_attempts_total{result=ok|notmodified|late|failed}`
- `rainalert_fetch_bytes_total`, `rainalert_daily_budget_used_ratio`
- `rainalert_decode_seconds`, `rainalert_pipeline_seconds` (alert if > 120 s — the 5-minute budget)
- `rainalert_subscriptions_active`
- `rainalert_evaluations_total{decision=…}`
- `rainalert_notifications_total{status=…}`
- `rainalert_missing_fraction` histogram (radar outage visibility)
- `rainalert_timeline_gaps` — cycles missing from the last `TIMELINE_PAST_HOURS`; a non-zero value
  means the map shows holes

Logs: structured JSON, one summary record per cycle (nominal time, bytes, decode ms, subscriptions
evaluated, alerts queued/sent, skipped). A per-subscription debug log line is emitted only at
`DEBUG`.

Operational alerts (email to the operator): cycle age > 20 min, circuit breaker open, notification
failure rate > 20 % over 30 min, daily budget > 80 %.

---

## 16. Testing strategy

Unit tests must run **offline** and fast. No test ever touches `opendata.dwd.de`.

1. **Decoder golden test.** Fixture `tests/fixtures/DE1200_RV2609161355_trimmed.tar.bz2` (frames
   `_000`, `_060`, `_120` of the 2026-09-16 13:55 cycle). Assert the decoder's output equals
   `wradlib.io.read_radolan_composite` on the same file (wradlib is a test-only dep, D-21) — this has
   already been verified bit-identical once by hand, so a failure means real drift.
   Note wradlib returns `-9999.0` for no-data, **not** `NaN`, and exposes `meta['nodatamask']` as flat
   indices; compare against those. Assert member naming and header parsing, **not** "25 members" —
   the fixture holds three.
   Include the sentinel case explicitly: a cell of `0x29C4` must decode to missing, never to
   25.00 mm/5 min (§5).
1b. **Radar-dropout test.** Fixture `tests/fixtures/DE1200_RV_outage_20260915_1615-1630.tar.bz2` — a
   real two-cycle dropout of the Borkum radar. Assert, for a 2 km mask at 53.58 N 6.66 E: at 16:15
   `missing_fraction[0] == 0` but the forecast frames are fully missing, so the cycle is **not**
   evaluated as dry; at 16:20 and 16:25 every frame is missing and the state is left unchanged; at
   16:30 normal evaluation resumes. Hamburg in the same files must be unaffected throughout.
   *This is the regression test for the frame-0-only gate — a real case that defeats it.*
2. **Grid/georeferencing test.** Reference points against `wradlib.georef.get_radolan_grid`,
   tolerance half a cell; plus known city coordinates.
3. **State machine tests.** Table-driven over synthetic sequences: clean onset, showers, forecast
   retraction, radar outage mid-warning, location jump, quiet hours on/off, `min_gap` on/off. Every
   row of the §9 table has at least one test.
4. **Sampler tests.** Synthetic grids with a known blob; assert radius mask size and max semantics;
   out-of-coverage handling.
5. **Politeness tests.** HTTP mock asserting: one GET per cycle, conditional headers sent, backoff
   sequence on 429/503, attempt cap, circuit breaker, budget guard.
6. **Idempotency test.** Run the pipeline twice on the same cycle → one `radar_cycles` row, one
   `evaluations` row per subscription, one mail.
7. **API tests.** Double opt-in flow end to end with the console notifier; token expiry/single use;
   enumeration-safe responses; deletion cascades.
8. **Rendering test.** Overlay PNG has expected size/bounds; colour scale maps known values;
   analysis and forecast frames land under the right prefixes.
9. **Timeline manifest test.** Frames ordered by `offset_minutes`; exactly one observed frame per
   cycle; forecasts only from the latest cycle; `kind` correct either side of the *now* boundary;
   missing cycles reported in `gaps` rather than silently skipped; `past_hours` clamped to
   `TIMELINE_PAST_HOURS`.
10. **Manual acceptance.** `rainalert probe --lat --lon` CLI prints the 25 lead values for a point;
   compare against a public radar map during actual rain before trusting the alerts.

CI: ruff + mypy (strict on `rainalert/`) + pytest + the import-linter rule that runtime code never
imports wradlib. Container image built and smoke-tested (`--help` on both entrypoints).

---

## 17. Repository layout

```
RainForecastWarning/
  pyproject.toml            # uv/poetry; groups: runtime, dev(+wradlib), test
  Dockerfile                # one image, entrypoints: api | ingest | cli
  Makefile                  # dev, test, lint, run-api, run-ingest, fixtures
  docs/DESIGN.md            # this document
  docs/DWD_RV_FORMAT.md     # M0 spike findings (authoritative once written)
  infra/                    # terraform or gcloud scripts, GCS lifecycle json
  migrations/               # alembic
  rainalert/
    config.py               # pydantic Settings (§14)
    db/                     # models, session, repositories
    radar/
      client.py             # politeness-enforcing DWD HTTP client (§4.3)
      decoder.py            # header + payload → arrays (§5)
      grid.py               # DE1200 ↔ WGS84, radius masks (§5)
      overlay.py            # reprojection + PNG rendering, obs/fc split (§11.1)
    alerting/
      sampler.py            # §8
      rules.py              # threshold/lead evaluation
      state_machine.py      # §9 — pure functions, no I/O
      dispatcher.py         # queue + deliver
    notify/
      base.py console.py smtp.py brevo.py push.py   # §12
    api/
      main.py routes_*.py templates/ static/
    jobs/
      ingest.py             # the Cloud Run job entrypoint (§6)
      retention.py          # purge job (GCS lifecycle + evaluations TTL)
      backfill.py           # past cycles from DWD / re-render from raw archives (§11.1)
      verify.py             # prediction vs observation (§9)
    cli.py                  # probe / backfill / render-backfill / send-test-mail
  tests/
    fixtures/ unit/ integration/
```

`state_machine.py` and `rules.py` must be **pure** (no DB, no clock, no network — time is injected).
That is what makes §16.3 cheap to write and trustworthy.

---

## 18. Milestones

Each milestone ends with a working, demonstrable artefact.

**M0 — DWD RV spike (half a day, do this first).**
Download a real RV archive by hand. Document in `docs/DWD_RV_FORMAT.md`: exact `_LATEST` filename,
inner member names and count, every header field with an example, the `PR` precision value, the flag
bit meanings, file size, observed publication delay over a couple of hours, and **how far back the
`rv/` directory retains files** (this decides whether the 12 h timeline can be backfilled at deploy
time or only accrues — §11.1).
*Done when:* the doc exists and a trimmed fixture is committed.
*This resolves every "VERIFY" marker in this document; if reality differs from §4.1, update §4/§5
before writing code.*

**M1 — decoder + grid + CLI.** ✅ *done 2026-09-16*
`decoder.py`, `grid.py`, `rainalert probe --lat 50.1 --lon 8.7` prints the lead values with valid
times. Tests §16.1, §16.2, §16.4 plus the dropout regression (§16.1b) — 32 tests, lint clean.
Grid verified against wradlib over all 1 320 000 cells.
*Remaining for the acceptance criterion:* probe output still needs to be eyeballed against a public
radar map during real rain. The fixtures prove internal consistency, not that the map is right.

**M2 — ingest pipeline.** 🟡 *code complete 2026-09-16, acceptance run outstanding*
Politeness client, archiving, `radar_cycles`, idempotency, advisory lock, the §4.3.1 validation
gates, retention. `make run-ingest` runs one cycle; 76 tests pass, including 14 politeness tests and
an ingest suite against a real Postgres.
*Done when:* 24 h of unattended running produces exactly 288 cycle rows, zero duplicate downloads,
and the politeness tests pass.
*Outstanding:* the 24 h run — and any run at all against the real `opendata.dwd.de`, which the
development environment cannot reach. The pipeline has only been exercised end to end against a
local server replaying a real archive. **The first live run is therefore itself a test**: watch the
first few cycles rather than scheduling it and walking away.
*Deliberately deferred:* Prometheus metrics (the endpoint belongs to the API service in M3; for now
the per-cycle numbers go to structured logs); the GCS store is written but unexercised, there being
no bucket yet (M6); Alembic arrives with M3, when there is more than one table to migrate.

**M3 — subscriptions + mail.** 🟡 *code complete 2026-09-16*
Schema and Alembic migrations, API (§10) minus `/forecast` and `/overlays`, double opt-in,
subscribe/confirm/unsubscribe/privacy pages, `Notifier` adapters, rate limits, deletion. 98 tests.
Verified end to end against a live server: subscribe → confirmation mail → GET leaves the state
untouched → POST activates → API token works → unsubscribe deletes everything.
*Done when:* a friend can subscribe from a phone browser and unsubscribe with one click.
*Outstanding:* a real address on a real domain, which needs Q-1 (domain) and Q-4 (provider), plus
SPF/DKIM/DMARC. Until then the flow is exercised with the `file` notifier.
*Deliberately deferred:* the map picker is M5, so the subscribe form takes coordinates with a
browser-geolocation button; rule parameters exist as columns and API fields but are not in the UI
(D-14); `PATCH /subscriptions/me` and pause/resume are not implemented yet.

**Delivery is configuration, not code (Q-4).** Every provider worth using — Brevo, Mailgun,
SendGrid, Postmark, SES, or an ordinary mailbox — speaks SMTP, so the SMTP adapter covers all of
them and choosing one is a matter of host, port and credentials. A provider's HTTP API can be added
behind the same `Notifier` protocol later if its delivery telemetry justifies the coupling. The
`console` and `file` adapters make the whole opt-in flow testable without sending anything.

**Domains are configuration too (Q-1).** `PUBLIC_BASE_URL` and `MAIL_FROM` are the only places a
hostname appears; every link in every mail is built from the former. The defaults are working
localhost placeholders, so nothing is blocked on choosing a domain.

**M4 — alerting.** 🟡 *code complete 2026-09-17*
Sampler, rules, state machine, dispatcher, `evaluations`/`rain_events`/`notifications`, and the
verification job. 143 tests, including the §9 transition table row by row.
Verified end to end: a real DWD cycle, a subscriber at a point that is dry now and wet in an hour,
produced one warning mail with a working one-click unsubscribe - and a second cycle of the same
front produced none.
*Done when:* a real alert mail arrives before real rain, and a replay of a stored rainy day produces
exactly one mail per event.
*Outstanding:* the real-rain half needs live DWD access and a real mailbox. The replay half is
covered by tests but not yet by an actual stored day.
*Deliberately deferred:* `dry_clear_minutes` is a constant rather than per-subscription; the
onset-field optimisation stays unbuilt (§8.1).

**On `UNKNOWN`:** an earlier revision had the first cycle only *observe*, which left every new
subscription — and every subscription that had just moved — blind for five minutes. It now warns
immediately when the first observation is dry with rain approaching, because that is a complete
picture: we know it is not raining here and we know rain is coming. Only the `now_wet` case stays
silent, which is the case that matters — rain already falling tells us nothing about whether the
subscriber has just walked into it.

**M5 — map UI.**
Overlay renderer (obs + fc prefixes), `/api/v1/overlays/timeline`, backfill/re-render job, subscribe
page with map picker, rain timeline overlay, slider, staleness banner, attribution.
*Done when:* the slider animates −12 h … +2 h smoothly on a phone over mobile data, gaps render as
gaps, and the observed/forecast boundary is unmistakable.

**M6 — deploy.**
Terraform/gcloud for all §6.1 resources, Secret Manager, SPF/DKIM/DMARC on the sending domain,
monitoring alerts, runbook in `docs/RUNBOOK.md`.
*Done when:* the service has run unattended for a week with cycle age < 20 min at all times.

**M7 — later (not v1).** Mobile app + FCM/APNs, user-editable rule UI, "all clear" mails, multiple
locations per subscriber, additional countries/sources.

---

## 18.1 Security findings still outstanding

An independent adversarial review is at [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) — 18 findings,
6 high, none critical. The findings that were design-level have been folded into the sections above
(§4.3.1, §6.1, §8.0, §8 step 4, §9, §15) and the two that were live code (F-1 decompression bomb,
F-3 undocumented exception types) are fixed with regression tests in `tests/test_hostile_input.py`
and `tests/test_grid.py`. The rest are tracked here so they are not lost, against the milestone that
owns them:

| Finding | Owner | Note |
|---|---|---|
| F-4 long-lived API token issued by an emailed `GET` link | M3 | Mail scanners GET links: the scanner consumes the single-use token *and* receives the bearer token. Make confirm a `POST` from a landing page; never put a token in a URL that gets logged |
| F-5 rate limiting has no defined client-IP source | M3 | `X-Forwarded-For` is attacker-controlled unless the trusted-proxy hop count is pinned. Deletion also erases the abuse state, so delete-and-retry resets any limit |
| F-6 scale-to-zero cost/DoS | M6 | Folded into §6.1 as `--max-instances`; the reserved-connection half is deploy work |
| F-8 Cloud Run request logs defeat §13 | M6 | The platform logs full URLs including query strings for 30 days by default — §13's "plaintext never stored" is only true once that is configured |
| F-9 "public-read or proxied" also exposes `raw/` | M6 | Split the bucket, or proxy; the raw DWD archives should not be world-readable next to the overlays |
| F-12 GDPR: DPA scope, consent columns, reversible IP hash | **now** | The mail-provider Auftragsverarbeitungsvertrag applies from the first friend's address, not from public launch. §7 has no columns for the consent record. An unsalted hash of an IP is reversible by brute force |
| F-13 `/metrics` "internal" is not expressible on Cloud Run | M6 | Either authenticate it or do not expose it |
| F-14–F-16 `/forecast` amplification, rule-parameter abuse, web hardening | M3/M5 | CSP `frame-ancestors`, `Referrer-Policy`, CSRF, mail header injection, session model |
| F-17 location updates silently suppress alerting for a moving user | M7 | D-17 resets state on a >1 km move; an app updating location often could keep a user permanently in `UNKNOWN` |
| F-18 supply chain and deploy path | M6 | Pin dependencies, pin base image by digest |

## 19. Open questions

| # | Question | Needed by |
|---|---|---|
| Q-1 | Domain name and sending domain (needed for links, `User-Agent` contact, SPF/DKIM/DMARC) | M3 |
| Q-2 | Confirm the private-audience assumption (D-18). Going public adds Impressum, Datenschutzerklärung, provider DPA | before any public link |
| ~~Q-3~~ | **Resolved 2026-09-16: Cloud SQL `db-f1-micro` in `europe-west3` for production, Neon or local Postgres for dev/CI.** Reasoning in §6.3 — the 5-minute cadence exhausts Neon's free CU-hour allowance around day 16 of each month, and Neon would add a second, US-headquartered processor for the table holding email plus home coordinates | done |
| Q-4 | Mail provider account: Brevo vs Mailgun vs SendGrid (all have a usable free tier) | M3 |
| Q-5 | Map tiles: OSM public tiles are fine privately but not for a public launch | M5 |
| Q-6 | Should raw archives be kept longer than 48 h — and become a permanent cold archive? They are the system of record (D-23), N-independent at ~500 GB/year, ≈ €2–4/month on Coldline, and the only thing that allows retroactively re-tuning thresholds against real weather. My recommendation: 48 h hot now, revisit once alerting is tuned | M2 |
| Q-8 | Is 12 h the right past span, or would 24 h be more useful? Storage is negligible (~22 MB per 12 h); the real limits are DWD's own file retention and slider usability | M5 |
| Q-9 | Accept the mail provider's DPA and Google's CDPA before the first friend subscribes (F-12). Ten minutes of clicking, and Art. 28 GDPR applies from the first address handed over — this is not launch paperwork | M3 |
| Q-7 | Reverse geocoding for a friendly place name in the subject line — worth an extra dependency/service? | M4 |

---

## 20. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| DWD changes the product/format again (as with `fx` → `rv`) | Service silently stops or mis-decodes | Golden fixture test (§16.1); cycle-age alert (§15); format details read from the header, not hard-coded |
| Georeferencing off by a few cells | Warnings for the wrong place, undetectable by eye | Cross-check against wradlib at fixed reference points (§16.2) |
| Nowcast skill decays with lead time | False alarms | 30 min default (D-13); verification job measures the real hit/false-alarm ratio; thresholds are per-subscription |
| Radar outage read as "dry" | Missed warnings, wrong state transitions | `MISSING_FRACTION_LIMIT` gate (§9 step 0) |
| Mail lands in spam | Service is useless | SPF/DKIM/DMARC as an M6 gate; transactional provider; RFC 8058 unsubscribe |
| Cloud Run job overruns the 5-minute budget as subscribers grow | Cycles skipped | `rainalert_pipeline_seconds` alert; split the overlay renderer out first (§6) |
| A poisoned or corrupt upstream response suppresses everyone's alerts while monitoring stays green | Nobody is warned, and we do not find out | Plausibility gate and cycle status metric (§4.3.1, §15); staleness alone does not cover "fetched, parsed, meaningless" |
| One unevaluatable subscription aborts every cycle | Permanent outage for all users from one bad row | Per-subscription fault isolation (§8 step 4); `OutsideGrid` covers every rejected coordinate |
| Hammering DWD through a retry bug | Blocked by DWD, reputational | Attempt caps, backoff, circuit breaker, daily byte budget, idempotency (§4.3) |
| Personal data leak (email + precise location) | GDPR incident | Minimisation, hashed tokens, rounded logs, hard delete (§13) |
| 168 timeline frames overwhelm a phone on mobile data | Map page unusable where it matters most | Windowed lazy loading, no full preload, LRU eviction (§11.1) |
| Per-subscriber time series outgrows the radar archive itself | DB cost scales with N × cycles for data that is ~99 % "nothing happened" | `evaluations` is a 48 h debug log; the permanent record is event-shaped (§8.1, D-23) |
| DWD retains too little history for backfill | Timeline starts empty and fills over 12 h | M0 verifies actual retention; re-render from raw archives; purely cosmetic — alerting is unaffected |

---

## 21. References

- DWD Open Data portal — https://www.dwd.de/DE/leistungen/opendata/opendata.html
- DWD radar open data root — https://opendata.dwd.de/weather/radar/
- RV composite directory — https://opendata.dwd.de/weather/radar/composite/rv/
- DWD RADVOR product page — https://www.dwd.de/EN/ourservices/radvor/radvor.html
- DWD radar products overview — https://www.dwd.de/EN/ourservices/radar_products/radar_products.html
- wradlib RADOLAN guide — https://docs.wradlib.org/projects/radolan/en/latest/
- `wradlib.io.read_radolan_composite` — https://docs.wradlib.org/en/2.3.0/generated/wradlib.io.radolan.read_radolan_composite.html
- wradlib issue #448 (DE1200 1200×1100 support) — https://github.com/wradlib/wradlib/issues/448
- Go RADOLAN/RADVOR parser (useful second opinion on the binary format) — https://github.com/jonnyschaefer/radolan
- Home Assistant DWD precipitation integration (prior art: RV usage, late-file handling) — https://github.com/evgparen/ha-dwd-precipitation
- DWD `content.log` tooling — https://github.com/DeutscherWetterdienst/opendata-content.log-tool
- RFC 8058 (one-click unsubscribe) — https://www.rfc-editor.org/rfc/rfc8058

# RainAlert runbook

What to do when something is wrong, and what to do to get it running in the first place.

The failure that matters is **nobody gets warned** — and its defining property is that it is
quiet. The service looks fine, the page loads, the map may even show old rain. Everything below is
organised around that.

---

## 1. First deploy

The first deploy does not need a domain, a certificate or a mail provider. Cloud Run serves
`https://rainalert-<hash>-ey.a.run.app` with a managed certificate, and push works without any
of the mail machinery — so the push-only path below is the short one, and email is a later,
separate step.

That https matters beyond tidiness: the session cookie's `Secure` flag is set from
`PUBLIC_BASE_URL` (`app.py`), and browsers refuse geolocation outside a secure context, so the
locate control on both maps cannot work over plain http at all.

### Push only — no domain, no mail provider

| # | What | Why it blocks |
|---|---|---|
| 1 | GCP project `rainchecker-195519`, billing enabled | nothing runs without it |
| 2 | Google's CDPA accepted | Art. 28 GDPR applies from the first subscriber, not from public launch |

Cloud Shell is enough for all of it — the image is built by Cloud Build, not locally, so nothing
here needs Docker on your machine.

```sh
# 0. Get the code there, and tell gcloud which project it is working on. Both are easy to skip
#    and both make the next command fail: `make image-push` needs a Makefile to be in, and
#    `gcloud builds submit` needs a project. Clone it - do not upload a zip - because the image
#    is tagged with `git rev-parse --short HEAD`.
git clone https://github.com/tschweitzer/RainForecastWarning.git
cd RainForecastWarning
gcloud config set project rainchecker-195519

# 1. Turn the APIs on. Terraform does this itself for everything except the first two, which
#    it cannot: enabling an API is a call to the Service Usage API, and reading which are
#    enabled is a call to Cloud Resource Manager, so on a project that has never used them the
#    first apply fails with SERVICE_DISABLED on every API at once. Doing the whole list here
#    also avoids the propagation wait - a freshly enabled API can 403 for a minute or two.
gcloud services enable \
  serviceusage.googleapis.com cloudresourcemanager.googleapis.com \
  cloudbuild.googleapis.com run.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com monitoring.googleapis.com logging.googleapis.com

# 2. Artifact Registry, once
gcloud artifacts repositories create rainalert \
  --repository-format=docker --location=europe-west3 --project=rainchecker-195519

# 3. Pin the base image by digest before the first build (SECURITY_REVIEW.md F-18). This
#    prints the FROM line to paste into the Dockerfile; a tag is mutable, a digest is not.
make pin-base

# 4. Build and push. This prints the digest - deploy by digest, never by tag
make image-push REGION=europe-west3 PROJECT=rainchecker-195519

# 5. Create infra/terraform.tfvars from the example and fill it in, including that digest.
#    The file is gitignored and does not exist in a fresh clone - it holds the digest and, once
#    email is on, the provider's details. Leave smtp_host out: that is what makes it push-only.
cd infra
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init && terraform apply

# 6. Read the service's URL, put it in terraform.tfvars as public_base_url, and apply again.
#    Leave that variable out for pass 5 rather than inventing a value: it is every link in
#    every message and the overlays bucket's only allowed CORS origin, so a made-up hostname
#    is a service whose links go nowhere and whose map shows nothing, with no error anywhere.
#    Put it in the file, not at an interactive prompt - a prompted value is not saved, so the
#    next apply asks again and silently reconfigures the service with whatever is typed then.
terraform output api_url
# or, straight from the source, which also works when the output is empty:
gcloud run services describe rainalert-api --region europe-west3 --format='value(status.url)' 
```

An apply that fails part-way is safe to re-run: Terraform is idempotent, and what it already
created (service accounts, buckets, IAM bindings) is simply adopted on the next pass.

**Except for a Cloud Run resource whose creation failed.** Terraform marks it tainted, which
plans as destroy-then-create, and the provider refuses the destroy unless `deletion_protection`
is false *in state*. It is false in `run.tf`, but a replace never writes the new value first —
the provider reads the flag from the prior state, where it is still true. So the apply loops on
the same two errors no matter how many times it is run:

```
Error: cannot destroy service without setting deletion_protection=false and running `terraform apply`
Error: cannot destroy job without setting deletion_protection=false and running `terraform apply`
```

Clear the taint instead of trying to satisfy the destroy. An update in place needs no destroy, so
it writes `deletion_protection = false` into state on the way past and the trap does not close
again:

```sh
terraform untaint google_cloud_run_v2_service.api
terraform untaint google_cloud_run_v2_job.ingest
terraform untaint google_cloud_run_v2_job.migrate
terraform plan        # expect "update in-place", no destroy
terraform apply
```

If the plan still shows a replace, or the apply 404s because the object was never really created,
drop it from state and let the next apply make it: `terraform state rm <address>`. That removes
Terraform's record, not the resource, so check with `gcloud run services list --region
europe-west3` and `gcloud run jobs list --region europe-west3` first — if it does exist, delete it
by hand before applying, or the create collides with it.

Migrations run by hand, deliberately — an automatic migration on container start means a rollback
can find a schema from the future:

```sh
gcloud run jobs execute rainalert-migrate --region europe-west3 --wait
```

The migration job also grants the web tier read/write on what it creates. Two database roles is
the design (F-6) and Postgres does not share ownership, so without that grant every page that
touches a table answers 500 while the container stays healthy and the ingest job keeps working.
Re-run the job after any migration; it is the only thing that maintains those grants.

Then subscribe yourself from a phone and confirm the notification arrives. Nothing is proven
until it does.

### Adding email later

| # | What | Why it blocks |
|---|---|---|
| 1 | A mail provider account (SMTP host, user, password) | no confirmations, no warnings |
| 2 | **SPF, DKIM and DMARC on the sending domain** | without them the warnings land in spam, which is the same as not sending them |
| 3 | The provider's DPA accepted | Art. 28 again, for a second processor |

```sh
# The SMTP password is the one secret Terraform does not generate. The secret already exists
# and is empty; this gives it a version.
echo -n 'the-password' | gcloud secrets versions add rainalert-smtp-password --data-file=-

# Set smtp_host (and mail_from, smtp_username) in terraform.tfvars, then apply. That one
# variable mounts the secret, sets EMAIL_CHANNEL_ENABLED, and puts the choice back on the
# signup page - they cannot drift apart because they are derived from it.
cd infra && terraform apply
```

### Watch the first few cycles

The first live run is itself a test: nothing in this repository has ever spoken to the real
`opendata.dwd.de`. Do not schedule it and walk away.

```sh
gcloud run jobs executions list --job rainalert-ingest --region europe-west3 --limit 5
gcloud run jobs executions logs read EXECUTION_ID --region europe-west3
```

Expect, per cycle: one `stored cycle …` line, one `cycle … evaluated` line, and nothing else. A
`304` line means no new cycle yet, which is normal between publications.

---

## 1b. Deploying a change

Every code change is the same four steps, and **the third is the one that is easy to skip**:

```sh
git pull                                                    # or commit your own work first
make image-push REGION=europe-west3 PROJECT=rainchecker-195519
#   -> prints:  image = "…@sha256:…"
#   paste that into infra/terraform.tfvars, replacing the old line
cd infra && terraform apply
```

Cloud Run is pinned to a digest (`var.image`), never a tag — that is what makes a rollback mean
something. The cost is that a build is not a deploy: `terraform apply` with the previous digest
still in `terraform.tfvars` redeploys the previous code, changes nothing, and **says "Apply
complete!"**. There is no error to notice. If a fix appears to have done nothing, check the
digest in `terraform.tfvars` against the one the build printed before looking anywhere else.

`make image-push` prints the whole `image = "…"` line for that reason: the next step is a paste,
not a transcription.

Two more things about that build:

- It uploads the **working directory**, not the commit, while tagging the image with `HEAD`. An
  uncommitted change gets built and labelled with the previous commit, so the tag becomes a lie
  about what is running. The target warns when the tree is dirty.
- **A migration is not deployed by the apply.** Changing the image makes the new migration
  *available*; running it is still `gcloud run jobs execute rainalert-migrate --region
  europe-west3 --wait`, by hand, after the apply.

---

## 2. "Is it actually working?"

The one question worth asking, and the order to ask it in:

```sh
# a. Is there a recent cycle? This is the SLI - everything else is downstream of it.
curl -sH "Authorization: Bearer $(gcloud secrets versions access latest \
  --secret=rainalert-metrics-token)" https://YOUR-HOST/metrics | grep cycle_age

# b. Is the job running at all?
gcloud run jobs executions list --job rainalert-ingest --region europe-west3 --limit 3

# c. Did anything get sent?
... | grep rainalert_notifications_24h
```

`rainalert_cycle_age_seconds` above ~1200 means warnings have stopped. `-1` means no cycle has ever
been stored.

`rainalert_cycle_age_negative 1` means a cycle is stamped in the future. Treat it as an integrity
problem, not a clock problem: that value comes from the file header, i.e. from the other side, and
a future stamp silences the staleness alarm until real time catches up.

---

## 3. Symptoms

### No warnings are going out

Work down this list; each step rules out one cause.

1. **Is the job running?** If executions stopped, look at Cloud Scheduler — an OIDC permission
   change is the usual cause.
2. **`ingestion halted` in the logs?** Either a byte budget is spent or the circuit breaker is
   open. Both are deliberate refusals to keep talking to DWD, and both mean nobody is being warned.
   The hourly budget exists so this costs an hour rather than a day. Check whether DWD is actually
   down before raising either limit.
3. **`blast radius` in the logs?** One cycle would have warned more than half the subscribers, so
   it queued nothing and asked for a human. Look at the map for that cycle. A genuine national
   squall line is real; a cycle that reads as rain over the whole country is likelier to be a
   broken cycle. If it is genuine, re-run the job for that cycle.
4. **`skipped_missing` for everyone?** The radar composite has no data at the subscribers'
   locations. Compare with DWD's own app. This is the correct behaviour — missing data must never
   be reported as dry — but if it persists for hours, DWD has a problem and so do we.
5. **Mail failing?** `rainalert_notifications_24h{status="queued"}` climbing while `sent` does not
   means delivery is broken, not evaluation. Check the provider's dashboard and the daily quota.

### Warnings go out but arrive late or not at all

Delivery, not evaluation. A notification older than 30 minutes is deliberately **expired rather
than sent** — a late rain warning is worse than none. Look for `expired` in the metrics.

### The map is empty or frozen

An empty frame that disappears on reload, with the timeline serving frames and the ingest job
storing cycles, is the Content-Security-Policy blocking the overlay PNGs. `img-src` is derived
from what is configured, and it has to name the overlay bucket as well as the tile provider -
the browser console says so plainly. Local development cannot reproduce it: `LocalOverlayStore`
serves overlays from the app itself, which is already `'self'`.

**The radar draws but the land underneath is blank.** On the vector map (the default since D-59), see §3c first: the tile server or the CSP. What follows is the raster basemap, which only the Leaflet fallback draws. That is the basemap, not the radar, and the
network tab tells the cases apart:

- **Tiles 404, or return something that is not an image.** The tile URL is wrong. Run
  `make verify-basemap`, which substitutes one tile and reports the status and content type. For
  basemap.de check the path component by component: the layer name
  (`de_basemapde_web_raster_farbe`), the tile matrix set (`GLOBAL_WEBMERCATOR`, *not*
  `DE_EPSG_25832_ADV`, which is UTM32 and will not line up with Leaflet), and the `.png`
  extension.
- **Tiles return 200 and the map is a jumble** - real coastlines and roads, none of them where
  they belong. The placeholder order is reversed. basemap.de is WMTS and wants `{z}/{y}/{x}`;
  OSM-derived providers want `{z}/{x}/{y}`. Nothing errors, because every request is for a tile
  that exists.
- **Tiles are fine inside Germany and blank outside it.** Working as intended. basemap.de covers
  Germany only, and so does the radar.


The alerting path and the map path are independent: the map can be broken while warnings still go
out, and that is the better failure of the two. Check that overlays are being written
(`gsutil ls gs://…-rainalert-overlays/obs/ | tail`) and that the manifest reports a recent
`latest_cycle`. `make rerender` rebuilds the history from archives already held — it does not talk
to DWD.

### A subscriber cannot sign up: "Dein Browser nutzt einen Push-Dienst, den wir noch nicht unterstützen"

The endpoint their browser issued is on a host `notify/webpush.py:ALLOWED_PUSH_HOSTS` does not list.
The log names it:

```sh
gcloud run services logs read rainalert-api --region europe-west3 --limit 200 | grep "subscribe refused"
#   subscribe refused: 'jmt17.google.com' is not a known push service
```

Add the host, or its family if the name carries a shard number, and deploy. `jmt<n>.google.com` is
already covered by `_GOOGLE_SHARD`.

**Think before widening.** The allowlist is what stops the subscribe endpoint being a server-side
request forgery primitive: `endpoint` is a URL a stranger supplies and this service POSTs to it. Add
the specific host or the specific family, anchored at both ends. Do not add a bare domain suffix -
`.google.com` would admit every Google host, not the push ones.

This happened, and the reason it took a week to find is worth keeping: the list had been written
from what the documentation says Chrome uses, not from what Chrome was observed to emit. Chrome
handed out `jmt17.google.com`, every Chrome subscriber on a shard was refused, the browser half of
the signup succeeded so their browser showed the site as subscribed, and the page told them to check
input that was already correct. Nothing was logged. If a browser you have not personally tested is
reported as broken, look here first.

### "RainAlert: radar data is stale"

The newest radar cycle is older than `timeline_stale_after_minutes` (20), or is stamped in the
future. Either way the map is not showing the current situation and nobody is being warned about
weather that is happening.

**The ingest job is probably healthy** - that is the point of this alert. It exists for the one
failure the other two cannot see: a run that fetches, gets a 304 and exits cleanly satisfies both
`ingestion_halted` and `job_not_completing` while the data quietly ages, which is exactly what DWD
stopping publishing looks like from here.

    gcloud run jobs logs read rainalert-ingest --region europe-west3 --limit 50

* Repeated `no new cycle (304)` - DWD has stopped publishing. Nothing to fix on this side; it
  resolves itself and the alert auto-closes after an hour.
* `cycle timestamp is in the future` - their clock or their filename is wrong. This is alerted
  separately on purpose: a future stamp reads as "the freshest data we ever had", so an alert on age
  alone would stay silent until real time caught up (SECURITY_REVIEW.md F-7).
* Neither, and cycles are arriving - then the threshold or the alert is wrong, not the pipeline.

**Why a log line and not a metric.** The SLI (`rainalert_cycle_age_seconds`) is computed from the
database and served at `/metrics`, which Cloud Monitoring cannot reach. The textbook fixes - Managed
Service for Prometheus, or a scheduled job writing a custom metric - each add a billable resource to
a stack whose point is being cheap. The ingest job already runs every five minutes, so it logs the
number and a log-based metric filters the words. Cost: nothing.

That makes the log text an interface. `tests/test_outage.py` asserts the phrases in
`infra/monitoring.tf` against the phrases actually logged, because rewording either side alone
leaves a metric that never increments and an alert that never fires, with nothing failing anywhere.

**Still not covered:** a deployment that has never ingested anything. `log_cycle_staleness` is silent
with no cycles at all, because a fresh project is empty between the `migrate` job and the first
ingest run and paging then would teach an operator to ignore this alert on the one day they are
certainly watching. `job_not_completing` covers a first run that never happens.

### `LookupError: '…' is not among the defined enum values. Enum name: channel`

A subscriber row holds a channel the code no longer knows. Seen on 2026-10-05 as `'NTFY'`: an ntfy
subscriber that the migration meant to delete it (`f3b8c21e7a94`) had missed, because it deleted
lowercase `ntfy` and `subscribers.channel` stores the uppercase enum *name*.

What it costs is the important part. `_persist` loads the subscriber only on the alert path, so it
raised precisely when rain approached that subscriber's location - and until 2026-10-05 it took
every other subscriber's warning in that cycle with it, permanently, because the cycle row is
committed before evaluation and is never re-evaluated. Persisting is now isolated per subscription
in a SAVEPOINT, so the same row today costs one subscriber, is marked `unhealthy`, and is logged.

The fix for the data is migration `a9e4d2c71f05`, which runs with the normal migrate step and prints
what it found (always, including zero). To check by hand in Cloud SQL Studio:

    SELECT channel, count(*) FROM subscribers GROUP BY channel;

Anything other than `EMAIL` or `WEBPUSH` - including the lowercase forms - is a row the ORM cannot
load.

**Writing raw SQL against `channel`:** it is uppercase, and it is the only enum column here that is.
Every other enum (`subscription_status`, `alert_state`, `cycle_status`, `token_purpose`) stores the
lowercase value. Lowercase against `channel` matches nothing and reports success.

### Tapping a notification does nothing at all

The reader taps a notification and the page they are already looking at does not change. The tell,
which is what makes this diagnosable at all: navigating somewhere else first and *then* tapping the
notification works.

This is one bug with one cause, and it has now appeared on three pages. Every token this service
issues rides in the URL **fragment** (D-26), because a fragment never reaches the server. Every page
that receives one reads it at load and erases it with `replaceState`, so a tab sitting on that page
has a bare path in its address bar. `focusOrOpen` in `sw.js` then reuses that tab by navigating it —
deliberately, because opening a window instead left one tab per notification. When the target path
equals the open tab's path, `client.navigate()` changes only the fragment, and **that is a
same-document navigation**: no script re-runs, the token is never read, nothing happens.

The fix is always on the page, never in the worker: a `hashchange` listener that re-reads the
fragment. `tests/test_pages.py::test_every_page_a_notification_can_open_re_reads_its_fragment`
enumerates the target paths from the `click_url`s `mail.py` builds and asserts each page has one, so
a new notification target fails the suite until its page can be re-entered.

Reported instances, for shape recognition:

| Page | Symptom |
| --- | --- |
| `/` | A second warning opened the country view, or stayed on the first warning's place |
| `/manage` | "Link an diesen Browser senden", stay on the page, tap the notification — nothing |
| `/confirm` | Found while fixing the above, unreported. Costlier: a signup that is never confirmed is purged after `unconfirmed_purge_hours` and the reader is never told why |

If a fourth page ever receives a token, it needs the same three lines. Two things make a naive fix
insufficient, both learned the hard way:

* **Re-running the page's bootstrap must be safe.** `manage.html` builds a map (MapLibre, or Leaflet as the fallback), and
  `L.map()` on an already-initialised container throws `Map container is already initialized`, which
  kills the rest of the handler. `buildMap()` returns early and re-places the pin instead.
* **The re-entry guard must queue, not discard.** A flag that simply returns while a run is in
  flight drops a fragment that arrives during a slow redeem — the same "nothing happened", rarer and
  harder to report. `restart()` schedules exactly one more pass.

### A subscriber says they got nothing

```sql
-- what the service decided for them, most recent first
select evaluated_at, now_wet, first_hit_lead_minutes, decision, state_before, state_after
from evaluations e join subscriptions s on s.id = e.subscription_id
where s.lat between :lat - 0.02 and :lat + 0.02 order by evaluated_at desc limit 20;
```

Remember this table is a **48 h window** (D-23). Beyond that, `rain_events` and `notifications`
still hold what was warned and sent, but not the per-cycle reasoning.

If their subscription shows `unhealthy`, evaluation has been failing for them specifically and the
manage page will be telling them so.

---

### After a deploy, a browser still behaves like the old version

Scripts and styles are loaded by content-versioned URL (`/static/radar.js?v=<hash>`, DESIGN.md
D-57), so a changed file is a new URL and no browser can keep the old one. The pages themselves are
`no-cache`, so the next page load picks up the new URLs. If a browser still shows old behaviour:

- **The tab was open across the deploy.** It runs the scripts it loaded; a reload fixes it.
- **The deploy did not happen.** Compare the `?v=` in the page source with
  `sha256sum rainalert/api/static/<file> | cut -c1-12` from the commit you meant to deploy.
- **A file is referenced without `static_url()`.** `tests/test_assets.py` fails for that on the
  pages it checks; a new page or template must use it too.

### The first page load after a quiet spell is slow

Expected, within limits. The web service scales to zero (`min_instance_count = 0`), so after about 15
minutes without a request Cloud Run stops the last instance, and the next visitor waits for a new
one: the container starts, Python imports the app, and the startup probe on `/healthz` has to pass
before the request is let through. The database plays no part - Cloud SQL never scales to zero, and
the ingest job queries it every five minutes anyway.

Two settings in `infra/run.tf` keep this short (D-51): the probe runs every second from the start,
so a ready app waits at most a second for it, and `startup_cpu_boost` doubles the CPU during
startup. Neither costs anything worth counting.

To see how long it actually is: Cloud Run → `rainalert-api` → Metrics → *Container startup
latency*. A few seconds is normal. If it is much longer, look at the revision's logs from the
start of the instance before changing anything here.

What would remove cold starts, and why it is not done:

- `min_instance_count = 1` in `run.tf`: always warm, but an idle instance is billed all month -
  several euros, which is a large share of this stack's bill.
- Having the ingest job request `/healthz` on each run would keep an instance warm for nearly
  nothing, but Cloud Run does not promise to keep idle instances, so it makes cold starts rare
  rather than impossible. Worth doing only if the metric above says the fix in place is not enough.

## 3b. Putting a custom domain in front

Cloud Run's own domain mapping is **not available in europe-west3**. The console says so outright:
"Domain mappings are not available in the region of the selected service. Either copy this service
to a different region, use an Application Load Balancer or Firebase Hosting." `gcloud beta run
domain-mappings list --region europe-west3` is not a test for this - it returns `Listed 0 items.`
either way.

Of the three, only two are real here:

* **Copy to another region** - no. europe-west3 is deliberate (German subscribers' data in Germany,
  `variables.tf`), and Cloud SQL is in the same region, reached over a unix socket. Moving the
  service away from its database for a nicer URL is the wrong trade.
* **Application Load Balancer** - works in any region, and the forwarding rule alone is roughly
  €15-20/month. That is more than the rest of this stack.
* **Firebase Hosting** - a `run` rewrite, works regardless of region, and gives a free
  `<site>.web.app` with a managed certificate before any domain is bought. This is the intended
  route.

### The two changes that are not `public_base_url`

Both are easy to miss because nothing fails loudly.

1. **`trusted_proxy_hops` 1 -> 2.** Firebase Hosting is a second proxy, so `X-Forwarded-For` gains
   an entry and `client_ip()` would otherwise return the Firebase edge address for every visitor -
   one shared rate-limit bucket, and `subscribe_limit_per_hour` becomes a global cap of five signups
   an hour. It is a variable so this is a one-line tfvars change that lands *with* the cutover;
   deploying 2 before Firebase is in front is the same outage in the other direction.

   Verify it rather than trusting the arithmetic - the chain is not obvious. Cloud Run's front end
   appends the address it saw, which is Firebase's edge, so `X-Forwarded-For` should arrive as
   `<visitor>, <firebase edge>` and the second-from-right entry is the visitor. Should. If Hosting
   adds more than one entry of its own, the count is 3 and 2 is wrong.

   **Measured on the 2026-10 cutover: 2 is correct** - the address recorded in the bucket was the
   phone's own public IP, so the chain is `<visitor>, <firebase edge>` as expected and limits are
   per-visitor. Re-measure if anything in front of the service changes.

   **The decisive check is one device.** Sign up, then:

       SELECT DISTINCT bucket FROM rate_limit_hits
       WHERE bucket LIKE 'subscribe:ip:%' ORDER BY 1;

   Compare the address in the bucket against `curl -s https://ifconfig.me` from the machine that
   signed up. The same address means the hop count is right. A Google-owned address means every
   visitor is being recorded as the CDN and they all share one bucket.

   Two things that look like this check and are not:

   * `SELECT DISTINCT bucket, occurred_at ...` - adding the timestamp makes every row distinct, so
     two signups always produce two rows whatever the bucket says. It answers nothing.
   * Two devices on the same WiFi - they share one public IP, so a single bucket is correct and
     expected. The two-network version of this test needs the phone on mobile data.

2. **Page cache headers.** Already handled - `page()` sets `private, no-cache` - but this is why:
   every page carries a per-request CSP nonce in both the header and the markup, and a shared cache
   passing one visitor's body to another either blocks every script on the page or hands out a nonce
   an injected script could claim. `/confirmed` also sets the session cookie. Do not "optimise" this
   to something cacheable.

### Firebase Hosting strips every cookie except `__session`

It drops all other cookies from the requests it proxies, so that it can cache: when `__session` is
present it goes into the cache key, which is what stops two visitors with different sessions being
served each other's response.

So the settings-page cookie **must** be named `__session` (`MANAGE_COOKIE` in `app.py`). It was
`rainalert_manage`, and the first cutover found out the hard way. The failure reports nothing
anywhere: the magic link redeems, the cookie is set, and then every request that needs it arrives
without one. `GET /api/v1/subscriptions/me` answers 401 and the page says *"Deine Einstellungen
konnten gerade nicht geladen werden. Fordere am besten einen neuen Link an."* - which reads as a
server fault and sends the reader to request another link that fails identically.

`tests/test_manage.py` pins the name and round-trips a real session, so this cannot regress quietly.

Related, and already handled: pages must not be cacheable by a shared cache (`page()` sets
`private, no-cache`), both because of the per-request CSP nonce and because `/confirmed` sets this
cookie. Do not "optimise" that header.

### Never run `rainalert reset` against the deployed database

It refuses now, and the reason it did not is worth knowing. `assert_local` allowed an empty hostname
so that a local unix socket would work - and production is exactly that shape, because Cloud Run
reaches Cloud SQL through the Auth proxy's socket at `/cloudsql/<project>:<region>:<instance>`
(`local.socket` in `infra/secrets.tf`). So the one guard in front of a `TRUNCATE` of every subscriber
table answered "local" for the live database. Nothing reached it, because there is no reset job in
`infra/`, but do not add one.

To clear test subscribers from the deployed database, use SQL in Cloud SQL Studio instead:

    -- what is there
    SELECT channel, status, count(*) FROM subscribers
      JOIN subscriptions ON subscriptions.subscriber_id = subscribers.id
      GROUP BY channel, status;

    -- cascades to subscriptions, auth_tokens, alert_states, evaluations, rain_events and
    -- notifications; leaves radar_cycles and rate_limit_hits alone (rate_limit_hits deliberately
    -- has no cascade - the record of abuse should outlive the account).
    DELETE FROM subscribers;

Repeatedly clearing site data while testing leaves one orphaned subscriber per cycle, each with a
push subscription the browser has already discarded. They are harmless - the liveness job retires
them after `webpush_liveness_days` - so this is tidiness, not repair.

### Running a query against the database

There is no psql on any machine here, and `authorized_networks` is deliberately empty
(`infra/main.tf`), so nothing may connect directly.

**Use Cloud SQL Studio**: console -> SQL -> `rainalert` -> Cloud SQL Studio. It needs a database
user and password, which live in Secret Manager:

    gcloud secrets versions access latest --secret=rainalert-api-database-url \
      --project rainchecker-195519

That prints the SQLAlchemy URL; the user is `rainalert_api`, the password is the part between `:`
and `@`, and the database is `rainalert`.

**Do not use `gcloud sql connect`.** It works by adding your current IP to the instance's
`authorized_networks` for a few minutes, which is exactly the setting this deployment leaves empty on
purpose - and Terraform will then want to remove it on the next apply, so it also shows up as drift.

Cloud Shell with `cloud-sql-proxy` is the other clean option if a real psql prompt is wanted.

### Order of operations

1. Create the Hosting site and the `run` rewrite. Confirm `https://<site>.web.app/` serves the page
   and `/healthz` answers.
2. *Then* set `public_base_url` to the new origin and `trusted_proxy_hops = 2`, and apply. Doing
   this first means every link sent in the meantime points at a host that does not serve the site.
3. Run the bucket query above.
4. Decide about the old origin. Push subscriptions belong to the origin that created them, so
   existing subscribers keep receiving warnings (the endpoint is at the push service, and VAPID's
   audience comes from that endpoint, not from us) but `/manage` on the new origin finds no
   registration and offers them the gate instead of their settings. If they then sign up again there
   are two live subscriptions for one person and both fire. While this is a handful of test
   subscribers the clean move is to delete the subscriber rows at cutover. Setting the service's
   `ingress` to load-balancer-only also stops the `*.a.run.app` origin being a second front door.

## 3c. The vector map

The start page and the settings page draw their maps with MapLibre on OpenStreetMap's vector
tiles (DESIGN.md D-58, the default since D-59). Leaflet with raster tiles (`MAP_TILE_URL`) is now
only the fallback.

- **Where the tiles come from:** `VECTOR_TILE_URL`, by default the OpenStreetMap Foundation's
  server (`vector.openstreetmap.org`, Shortbread schema), under its vector tile usage policy -
  best effort, no SLA, heavy users may be blocked without notice. Every visitor's browser fetches
  tiles from there directly, sending this site's origin as Referer.
- **Turning it off:** set `VECTOR_TILE_URL` to an empty string. Both pages then draw with Leaflet
  and `MAP_TILE_URL`, as before D-58. This is also the way back if the OSMF ever blocks the site.
- **Where a page falls back to Leaflet by itself:** no WebGL, a browser without module scripts,
  or MapLibre failing to load. Both pages load Leaflet as well for exactly that.
- **Open item: the OSMF vector tile usage policy.** It was not readable from the development
  sandbox, so nobody has checked that it covers this use - read
  <https://operations.osmfoundation.org/policies/vector/> and note the outcome here. The raster
  servers' policy rules apps out (DESIGN.md, basemap); the vector service's is separate. If it does
  not fit, point `VECTOR_TILE_URL` at another Shortbread provider or clear it.
- **Privacy:** the tile server sees each visitor's IP and map area - on the settings page, the area
  around their warning location. The privacy page names the configured hosts; change them and it
  follows by itself.
- **Changing the map's look:** the styles are generated, not hand-edited. `scripts/map-style/`
  builds `rainalert/api/static/map/gray.json` and `gray-dark.json` (the dark one is lightened
  there, D-59); see its `build.mjs`.
- **If the map is blank but the radar and the labels-free page work:** the tile server is not
  answering, or the CSP is blocking it. The browser console says which; the CSP's `connect-src`
  lists the tile server's origin, derived from `VECTOR_TILE_URL`.

## 4. Routine operations

### Schema changes

```sh
# generate locally against a scratch database, review the file, commit it
alembic revision --autogenerate -m "what changed"
# apply in production, by hand, before deploying the image that needs it
gcloud run jobs execute rainalert-migrate --region europe-west3 --wait
```

CI fails if the models and migrations have drifted, so a missing migration is caught before it
reaches here.

### Reverting the web push change specifically

`git revert` of the D-45 commit also reverts the migration *file*, so alembic's head drops back to
`d5a1c7e93b42` while the database is still at `f3b8c21e7a94`. The old code then meets
`push_p256dh`/`push_auth` columns it does not know and `channel = 'WEBPUSH'` rows it cannot route.

Downgrade the database **before** deploying the revert, not after:

```sh
gcloud run jobs execute rainalert-migrate --region europe-west3 --args=downgrade,-1 --wait
# then deploy the reverted image
```

The migration handles this correctly in both directions — it deletes the `webpush` rows on the way
down, because the older schema has nowhere to put their keys. It is the ordering that has to be
right, and it is the opposite of the usual "deploy, then migrate".

### Rolling back

Deploy the previous digest. Roll the schema back only if the new one is genuinely incompatible —
Postgres will happily serve an older application from a newer schema in most cases, and a hurried
`downgrade` on a live database is its own incident.

### Rotating `SECRET_KEY`

It signs the unsubscribe links, which are stateless. Rotating it **invalidates every unsubscribe
link already in someone's inbox**. Only do it if the key is believed compromised, and expect
support mail.

### Do not rotate the VAPID key

`rainalert-vapid-private-key` signs every web push send, and a push service checks each one against
the key the subscription was created with. Replace it and **every existing push subscriber silently
stops being warned**: the rejection is a 401 or 403, not the 410 that would tell us to delete the
row, so nothing is cleaned up and nothing is reported. Subscribers keep their notification
permission, see no error, and simply never hear from us again. There is no repair short of asking
every one of them to subscribe afresh, and no way to reach them to ask.

Terraform generates it (`tls_private_key.vapid` in `infra/secrets.tf`) and will not replace it on its
own, because the resource has no inputs that change. What *would* replace it is a `terraform taint`,
a `-replace=`, or losing the state file. If the state is ever lost, restore it from the bucket's
versions rather than re-applying: a fresh apply mints a new key and takes every subscriber with it.

If it genuinely has to be rotated - a real compromise - accept that push subscribers are gone, and
delete their rows so the database does not hold coordinates for people who can no longer be reached:

```sh
gcloud run jobs execute rainalert-migrate --region europe-west3   # nothing schema-related; just
# ... then, with the proxy up:
# UPPERCASE. `subscribers.channel` stores the enum *name*, unlike every other enum column here,
# which store the lowercase value - see `Channel` in rainalert/db/models.py. Lowercase matches no
# rows and reports success, which is how f3b8c21e7a94 deleted nothing on 2026-09.
psql -c "delete from subscribers where channel = 'WEBPUSH';"
```

### The liveness job

`rainalert-liveness` runs weekly (Wednesdays) and is the only mechanism that notices a push
subscriber who left without telling us — blocking notifications, clearing site data and uninstalling
the browser all revoke the subscription silently (D-46). It sends one notification to anyone who has
heard nothing for `WEBPUSH_LIVENESS_DAYS` (30), and deletes whoever the push service reports as
gone. Weekly rather than monthly because a monthly run does not bound silence at 30 days - somebody
quiet since just after a run is not yet 30 days quiet at the next one, so the first run that sees
them is the one after, about 60 days. Three runs in four find nobody due, which costs nothing.

Check what it would do before trusting it, which needs no schedule:

```sh
gcloud run jobs execute rainalert-liveness --region europe-west3 --wait
gcloud run jobs executions logs read --region europe-west3 --job rainalert-liveness --limit 50
```

Two failure modes worth knowing. If it has **never run successfully**, stale subscriber rows
accumulate and so do their stored coordinates — which is a retention problem, not just an
operational one. If it deletes **everybody at once**, suspect the VAPID key rather than the
subscribers: a rotated key makes every send fail, and while those failures are 401/403 rather than
410 and should *not* delete anyone, a mass deletion here is the signal to stop the scheduler and
look before the next run.

---

## 5. Known gaps

- ~~**The cycle-age SLI is not alerted.**~~ *Closed 2026-10-04.* It is still not on a dashboard —
  it lives in the database, which Monitoring cannot see, and putting it there needs something to
  scrape `/metrics`. But the *alerting* gap is closed without that: the ingest job already runs every
  five minutes, so `log_cycle_staleness` logs the age there and `google_logging_metric.stale_radar`
  alerts on the words. No new schedule, no new billable resource. Section 3 has the playbook, and
  the one case still uncovered (a deployment that has never ingested anything).
- ~~**Leaflet and OSM tiles are third-party.**~~ *Closed 2026-09-27.* Leaflet is vendored under
  `rainalert/api/static/vendor/leaflet` and served by this app, so `script-src` and `style-src` are
  back to `'self'` and no CDN sees a visitor. The basemap now defaults to basemap.de Web Raster
  (BKG, CC BY 4.0, no key, no quota, no non-commercial clause), so a visitor's IP reaches a German
  federal agency's tile service and nothing else. `make verify-basemap` checks that endpoint with
  one request — worth running once per environment, because a wrong WMTS path serves blank tiles
  rather than an error. Setting `map_tile_url` and `map_tile_attribution` to `""` restores the
  no-basemap state (radar over a graticule) if you want nobody at all in the request path.
- **Cloud Run logs full request URLs** for 30 days by default. No token is in one any more —
  confirm, unsubscribe, the magic link and a warning's location reference all ride in the URL
  fragment, which a browser never sends (D-26) — but the paths themselves still say who asked
  for what, and the logs carry client IPs.
- ~~**Nothing here has spoken to the real DWD server.**~~ *Closed 2026-10-04.* The ingest job runs
  against the real server on its 5-minute schedule and produces cycles. Every *test* still uses
  fixtures or a local replay, which is the right thing for a test suite — DWD is not a fixture — so
  format drift is still only caught by the golden-fixture test plus the §4.3.1 validation gates at
  runtime.
- **A real push service and a real phone: mostly verified, 2026-10.** What has happened for real,
  end to end on an Android phone and a desktop browser: the permission prompt, a subscription
  against FCM, a confirmation notification arriving and being tapped, and a settings-link
  notification arriving and opening `/manage` signed in. So FCM accepts our RFC 8291 bodies. Signup
  also succeeded on Firefox, Edge and Opera earlier, which exercises Mozilla's push service at least
  as far as `subscribe()`.

  Still unverified, and worth knowing which: **an actual rain warning has never been delivered to a
  real person** — every notification so far has been a confirmation or a settings link, which take a
  different path through `mail.py` and carry different actions. Also unverified: the notification
  *action* button (`Einstellungen`) being drawn and tapped, since the magic links so far were
  requested from the settings gate rather than from a warning; Apple's push service at all; and
  `pushsubscriptionchange` firing on a rotation. The first real warning is still the test — watch
  `notifications.status` when it fires.
- **No `Topic` header (RFC 8030 §5.4), deliberately.** A phone offline for 25 minutes comes back to
  several queued warnings; `Topic` would let the push service collapse them server-side so only the
  newest is delivered. It is not needed for the *reader's* experience, because the client-side `tag`
  already collapses them: each queued push wakes the worker, each `showNotification` under the same
  tag replaces the last, and what they see is the freshest one — which is the one they want. What
  `Topic` would actually buy is bandwidth: several encrypted payloads delivered to a metered radio
  instead of one. That is a real but small cost, and it is the whole of the argument.
  **Revisit this if the tags change.** The reasoning depends entirely on same-kind messages sharing
  a tag. They now use two tags (`rainalert-alert` and `rainalert-manage`, see `api/mail.py`), which
  preserves the collapse *within* a family; splitting further — per event, say — would break it and
  make `Topic` worth having.
- **There is no instrument for a push that is accepted and never displayed.** If a payload were
  encrypted to the wrong keys, the push service would still answer 201 and this service would record
  `sent`; the reader sees nothing and has nothing to report. `run_liveness` cannot catch it either,
  because it measures successful *sends*. Two things bound it, and they are worth stating because
  together they make the gap smaller than it first looks:
  1. **A confirmation proves the chain once, per subscriber.** A push subscriber cannot reach
     `confirmed_at` unless a notification was encrypted, delivered, rendered and tapped. So day-one
     breakage is impossible; what is left is *regression* — a VAPID rotation, a payload-shape change
     — after a subscriber is already confirmed.
  2. **The liveness ping already carries a tap.** It ships an Einstellungen button, and
     `POST /api/v1/manage/request` is reached only when a human presses it.
  `run_liveness` now logs `silent=<n>`: confirmed push subscribers with successful sends who have
  never had a `MANAGE` token issued. It is a smell, not an alarm — someone can simply never need
  their settings — but a number that climbs while sends succeed is the signature of this failure,
  and there was previously nothing at all to watch.
- **Nothing deletes a push signup whose confirmation token has expired.** `purge_unconfirmed` runs
  at `unconfirmed_purge_hours` and covers it, so the row does go — but `due_for_liveness` excludes
  unconfirmed subscribers by design, so until the purge runs the coordinates of somebody who never
  completed a signup are held with nothing watching them. Worth re-checking if the purge job's
  schedule ever changes.
- **iOS needs the site on the Home Screen** before web push works at all, which is a step the page
  describes and nobody here has performed. An iPhone user who does not do it gets no warnings and no
  error.
- **Desktop push only arrives while the browser is running.** Chrome, Firefox and Safari all launch
  the service worker for a push only if the browser process is alive, so a closed laptop misses
  warnings. Not a regression — the old browser route had the same ceiling — and not fixable.
- **The database has a public endpoint.** Nothing may connect to it — `authorized_networks` is
  empty and `ssl_mode` is `ENCRYPTED_ONLY`, so the only way in is the Cloud SQL Auth proxy
  authenticating as a service account with `roles/cloudsql.client`. Real network isolation means
  private IP, which needs a VPC, a private services access range and Direct VPC egress on all
  three workloads. Worth doing before this holds anyone's data but the author's.

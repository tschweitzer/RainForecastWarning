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


The alerting path and the map path are independent: the map can be broken while warnings still go
out, and that is the better failure of the two. Check that overlays are being written
(`gsutil ls gs://…-rainalert-overlays/obs/ | tail`) and that the manifest reports a recent
`latest_cycle`. `make rerender` rebuilds the history from archives already held — it does not talk
to DWD.

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

### Rolling back

Deploy the previous digest. Roll the schema back only if the new one is genuinely incompatible —
Postgres will happily serve an older application from a newer schema in most cases, and a hurried
`downgrade` on a live database is its own incident.

### Rotating `SECRET_KEY`

It signs the unsubscribe links, which are stateless. Rotating it **invalidates every unsubscribe
link already in someone's inbox**. Only do it if the key is believed compromised, and expect
support mail.

---

## 5. Known gaps

- **The cycle-age SLI is not on a Cloud Monitoring dashboard.** It lives in the database, which
  Monitoring cannot see; reaching it needs something to scrape `/metrics`. The two alert policies
  in `infra/monitoring.tf` catch the same failure from the outside (a halted or failing job stops
  producing cycles), so this is an observability gap rather than a safety one.
- **Leaflet and OSM tiles are third-party.** Every visitor to `/map` reveals their IP to unpkg and
  to OpenStreetMap. Vendor Leaflet into `static/` and choose a tile provider before any public use.
- **Cloud Run logs full request URLs** for 30 days by default. No token is in one any more —
  confirm, unsubscribe, the magic link and a warning's location reference all ride in the URL
  fragment, which a browser never sends (D-26) — but the paths themselves still say who asked
  for what, and the logs carry client IPs.
- **Nothing here has spoken to the real DWD server.** Every test uses fixtures or a local replay.
- **The database has a public endpoint.** Nothing may connect to it — `authorized_networks` is
  empty and `ssl_mode` is `ENCRYPTED_ONLY`, so the only way in is the Cloud SQL Auth proxy
  authenticating as a service account with `roles/cloudsql.client`. Real network isolation means
  private IP, which needs a VPC, a private services access range and Direct VPC egress on all
  three workloads. Worth doing before this holds anyone's data but the author's.

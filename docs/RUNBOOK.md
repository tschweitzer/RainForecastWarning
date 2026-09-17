# RainAlert runbook

What to do when something is wrong, and what to do to get it running in the first place.

The failure that matters is **nobody gets warned** — and its defining property is that it is
quiet. The service looks fine, the page loads, the map may even show old rain. Everything below is
organised around that.

---

## 1. First deploy

Prerequisites, in the order they block things:

| # | What | Why it blocks |
|---|---|---|
| 1 | GCP project `rainchecker-195519`, billing enabled | nothing runs without it |
| 2 | A domain, or the Cloud Run URL as a stand-in | every emailed link is built from `PUBLIC_BASE_URL` |
| 3 | A mail provider account (SMTP host, user, password) | no confirmations, no warnings |
| 4 | **SPF, DKIM and DMARC on the sending domain** | without them the warnings land in spam and the service is pointless |
| 5 | The provider's DPA accepted, and Google's CDPA | Art. 28 GDPR applies from the first friend's address, not from public launch |

Then:

```sh
# 1. Artifact Registry, once
gcloud artifacts repositories create rainalert \
  --repository-format=docker --location=europe-west3 --project=rainchecker-195519

# 2. Build and push, and note the digest - deploy by digest, never by tag
make image-push

# 3. Fill in infra/terraform.tfvars from the example, including that digest
cd infra && terraform init && terraform apply

# 4. The SMTP password is the one secret Terraform does not generate
echo -n 'the-password' | gcloud secrets versions add rainalert-smtp-password --data-file=-

# 5. Point PUBLIC_BASE_URL at the api_url output and apply again
```

Migrations run by hand, deliberately — an automatic migration on container start means a rollback
can find a schema from the future:

```sh
gcloud run jobs execute rainalert-migrate --region europe-west3 --wait
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
- **Cloud Run logs full request URLs**, including `?token=…` on confirm and unsubscribe links, for
  30 days by default. Shorten the retention or scrub the field before treating §13's "plaintext
  never stored" as true in production.
- **Nothing here has spoken to the real DWD server.** Every test uses fixtures or a local replay.

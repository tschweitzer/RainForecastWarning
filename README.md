# RainForecastWarning

A cloud service that warns you by browser push notification shortly before it starts raining at
your location, using DWD radar nowcast data (Germany).

**Status:** in production on Google Cloud (Cloud Run, Cloud SQL, Firebase Hosting). It ingests
DWD's radar composite every five minutes, evaluates each subscriber's location, sends a warning
before rain arrives ("Es regnet jetzt" when it starts unannounced), and shows a rain map with a
48 h … +2 h timeline slider.

Web Push is the only channel (DESIGN.md D-76): it needs nothing beyond the deployment and a VAPID
key — no mail provider, no account, no address of anyone's.

- **[docs/DESIGN.md](docs/DESIGN.md)** — requirements, decisions log, architecture, data model,
  alert state machine, API, privacy, testing strategy and milestones.
- **[docs/DWD_RV_FORMAT.md](docs/DWD_RV_FORMAT.md)** — the RV product as observed, not as documented:
  the HDF5 edition read since 2026-10-20 (§11a), the retired binary one, the grid and its
  georeference (§11), publication timing, retention.

## Quick start

```sh
make dev     # virtualenv + dependencies
make test    # needs TEST_DATABASE_URL; includes golden comparisons against wradlib and DWD twin cycles
make lint

.venv/bin/python -m rainalert.cli probe \
    tests/fixtures/composite_rv_20261018_1740_trimmed.tar --lat 50.1109 --lon 8.6821
```

`probe` prints what the service would see at one location for every forecast step of a cycle — the
manual check that the numbers are right before trusting an alert.

Running one ingest cycle needs a Postgres and somewhere to put archives:

```sh
export DATABASE_URL='postgresql+psycopg://user@host/rainalert'
make run-ingest ARCHIVE_DIR=./var/raw
```

It fetches the latest cycle conditionally, refuses anything that is not a plausible national
composite, archives the raw bytes, and records exactly one row per nominal time. The ingest tests
need a real Postgres and skip without `TEST_DATABASE_URL`. Set `REQUIRE_DATABASE_TESTS=1`
to turn that skip into a failure - CI does, so a broken database cannot pass as green.

Running the web service:

```sh
make migrate           # create/upgrade the schema
make serve             # http://localhost:8000
```

With `NOTIFIER=file` and `OUTBOX_DIR=./var/outbox`, notifications are written as `.txt` files —
title, body and the link a tap would open — instead of being pushed, and `rainalert outbox` prints
the newest. That is how the whole double opt-in flow can be exercised without a push service.

**Configuration you will need at deploy time** (all have working local defaults):

| Variable | What it is |
|---|---|
| `PUBLIC_BASE_URL` | the origin every link in a notification is built from |
| `NOTIFIER` | `webpush` in production; `console` or `file` locally |
| `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` | the push signing key (`rainalert vapid-keys`) and the operator contact push services see |
| `SECRET_KEY` | salts the consent IP hashes; must be set in production |
| `TRUSTED_PROXY_HOPS` | how many proxies in front of us are ours (0 ignores `X-Forwarded-For`) |
| `CONTACT_EMAIL` / `PRIVACY_CONTROLLER` | footer contact, and the controller's name and address the privacy page must name (D-72) |

```sh
make run-ingest        # fetch a cycle, evaluate every subscriber, send what is due
make verify            # score past warnings now (the ingest job also does it every cycle, D-73)
```

Set `OVERLAY_DIR` as well and the ingest job renders map frames; `/` then shows the timeline.

```sh
make rerender          # rebuild map overlays from archives already held (no DWD traffic)
```

**Want to try it?** [docs/LOCAL.md](docs/LOCAL.md) runs the whole service on a laptop against the
real DWD server — no cloud account and no domain. Notifications are written as text files.

Deployment lives in [`infra/`](infra/) (Terraform) with the procedure and failure modes in
[docs/RUNBOOK.md](docs/RUNBOOK.md).

Data basis: Deutscher Wetterdienst (DWD), radar product RV, CC BY 4.0.

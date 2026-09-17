# RainForecastWarning

A cloud service that warns you by email shortly before it starts raining at your location,
using DWD radar nowcast data (Germany).

**Status:** M0–M5 code complete; M6 deployment artifacts written but never applied. The service
works end to end locally: it ingests DWD radar, evaluates each subscriber's location, emails a
warning before rain arrives, and shows a rain map with a −12 h … +2 h timeline slider.

Deploying needs a domain, a mail provider with SPF/DKIM/DMARC, and its DPA accepted — see
[docs/RUNBOOK.md](docs/RUNBOOK.md).

- **[docs/DESIGN.md](docs/DESIGN.md)** — requirements, decisions log, architecture, data model,
  alert state machine, API, privacy, testing strategy and milestones.
- **[docs/DWD_RV_FORMAT.md](docs/DWD_RV_FORMAT.md)** — the RV format as observed, not as documented:
  header fields, the no-data sentinel and the trap it sets, publication timing, retention.

## Quick start

```sh
make dev     # virtualenv + dependencies
make test    # 169 tests, including a golden comparison against wradlib
make lint

.venv/bin/python -m rainalert.cli probe \
    tests/fixtures/DE1200_RV2609161355_trimmed.tar.bz2 --lat 50.1109 --lon 8.6821
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
need a real Postgres and skip without `TEST_DATABASE_URL`.

Running the web service:

```sh
make migrate           # create/upgrade the schema
make serve             # http://localhost:8000
```

With `NOTIFIER=file` and `MAIL_OUTBOX_DIR=./var/outbox`, mails are written as `.eml` files you can
open instead of being sent — which is how the whole double opt-in flow can be exercised locally.

**Configuration you will need at deploy time** (all have working local defaults):

| Variable | What it is |
|---|---|
| `PUBLIC_BASE_URL` | the origin every emailed link is built from |
| `MAIL_FROM` | sender address; its domain needs SPF, DKIM and DMARC or the mail lands in spam |
| `NOTIFIER` | `console`, `file` or `smtp` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | any provider — they all speak SMTP |
| `SECRET_KEY` | salts the consent IP hashes; must be set in production |
| `TRUSTED_PROXY_HOPS` | how many proxies in front of us are ours (0 ignores `X-Forwarded-For`) |

```sh
make run-ingest        # fetch a cycle, evaluate every subscriber, send what is due
make verify            # score past warnings against what the radar then saw
```

Set `OVERLAY_DIR` as well and the ingest job renders map frames; `/map` then shows the timeline.

```sh
make rerender          # rebuild map overlays from archives already held (no DWD traffic)
```

Deployment lives in [`infra/`](infra/) (Terraform) with the procedure and failure modes in
[docs/RUNBOOK.md](docs/RUNBOOK.md).

Data basis: Deutscher Wetterdienst (DWD), radar product RV, CC BY 4.0.

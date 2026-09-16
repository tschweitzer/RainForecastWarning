# RainForecastWarning

A cloud service that warns you by email shortly before it starts raining at your location,
using DWD radar nowcast data (Germany).

**Status:** M0 and M1 complete, M2 code complete. The radar format is verified against real data;
the decoder, georeferencing, `probe` CLI and the ingest pipeline exist. No subscriptions or web
service yet.

- **[docs/DESIGN.md](docs/DESIGN.md)** — requirements, decisions log, architecture, data model,
  alert state machine, API, privacy, testing strategy and milestones.
- **[docs/DWD_RV_FORMAT.md](docs/DWD_RV_FORMAT.md)** — the RV format as observed, not as documented:
  header fields, the no-data sentinel and the trap it sets, publication timing, retention.

## Quick start

```sh
make dev     # virtualenv + dependencies
make test    # 76 tests, including a golden comparison against wradlib
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

Next up is **M3**: subscriptions, double opt-in and the mail path.

Data basis: Deutscher Wetterdienst (DWD), radar product RV, CC BY 4.0.

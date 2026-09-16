# RainForecastWarning

A cloud service that warns you by email shortly before it starts raining at your location,
using DWD radar nowcast data (Germany).

**Status:** milestones M0 and M1 complete — the radar format is verified against real data and the
decoder, georeferencing and `probe` CLI exist. No service yet.

- **[docs/DESIGN.md](docs/DESIGN.md)** — requirements, decisions log, architecture, data model,
  alert state machine, API, privacy, testing strategy and milestones.
- **[docs/DWD_RV_FORMAT.md](docs/DWD_RV_FORMAT.md)** — the RV format as observed, not as documented:
  header fields, the no-data sentinel and the trap it sets, publication timing, retention.

## Quick start

```sh
make dev     # virtualenv + dependencies
make test    # 32 tests, including a golden comparison against wradlib
make lint

.venv/bin/python -m rainalert.cli probe \
    tests/fixtures/DE1200_RV2609161355_trimmed.tar.bz2 --lat 50.1109 --lon 8.6821
```

`probe` prints what the service would see at one location for every forecast step of a cycle — the
manual check that the numbers are right before trusting an alert.

Next up is **M2**: the ingest pipeline (fetching from DWD under the politeness rules in DESIGN.md
§4.3, archiving, and the cycle bookkeeping).

Data basis: Deutscher Wetterdienst (DWD), radar product RV, CC BY 4.0.

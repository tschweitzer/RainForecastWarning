# Running the whole thing locally (macOS)

Everything works on a laptop with no cloud account, no domain and no mail provider: archives go to a
directory, mail is written as `.eml` files you can open, and the map is served by the app itself.

**This is worth doing before any deployment**, because your machine can reach
`opendata.dwd.de` and the development environment this was written in could not. Nothing in this
repository has ever spoken to the real DWD server. The first local run is therefore a genuine test,
not a demo — watch it rather than leaving it running.

---

## 1. Prerequisites

macOS ships Python 3.9; this needs 3.11+.

```sh
brew install python@3.11 postgresql@16
brew services start postgresql@16
```

Postgres is required rather than SQLite: the ingest path uses advisory locks and array columns, and
testing those on SQLite would prove nothing. If you would rather not install it,
`docker run -p 5432:5432 -e POSTGRES_PASSWORD=rainalert -e POSTGRES_USER=rainalert -e POSTGRES_DB=rainalert postgres:16`
works just as well.

```sh
git clone https://github.com/tschweitzer/RainForecastWarning
cd RainForecastWarning
make dev            # virtualenv + dependencies
createdb rainalert  # Homebrew Postgres; with Docker the database already exists
```

## 2. Configuration

Create `.env` in the repository root:

```sh
DATABASE_URL=postgresql+psycopg://$(whoami)@/rainalert?host=/tmp
# Docker instead: postgresql+psycopg://rainalert:rainalert@localhost:5432/rainalert

ARCHIVE_DIR=./var/raw
OVERLAY_DIR=./var/overlays
NOTIFIER=file
MAIL_OUTBOX_DIR=./var/outbox
PUBLIC_BASE_URL=http://localhost:8000
SECRET_KEY=local-development-only

# Identify yourself honestly to DWD. This is a real request to a public service, and the
# contact address is how they reach you if something you do is a problem (DESIGN.md 4.3).
DWD_USER_AGENT=RainAlert/0.1 (local test; contact: you@example.org)
```

```sh
make migrate
```

## 3. The moment of truth: one real cycle

```sh
make run-ingest
```

Expect roughly:

```
{"level":"INFO","msg":"stored cycle 2026-09-17 10:25:00+00:00: 25 frames, 1327867 bytes, 1 attempt(s)"}
{"level":"INFO","msg":"cycle 2026-09-17 10:25:00+00:00 evaluated: 0 subscriptions, 0 alerts, ..."}
```

**25 frames** is the number to look for — every fixture in the test suite is a trimmed three-frame
archive, so this is the first time the real thing has been through the decoder end to end.

Things that would be genuinely interesting to hit here, and what they mean:

| What you see | What it means |
|---|---|
| `rejected: nominal time is N min in the future` | our clock or DWD's disagrees by more than 15 min |
| `rejected: no-data share X% outside the plausible band` | the plausibility gate firing on a real cycle — the band was calibrated from two archives, so it may need widening |
| `RVFormatError` | the format has drifted since `docs/DWD_RV_FORMAT.md` was written |
| `not_modified` on the very first run | nothing stored yet; run it again a few minutes later |

## 4. Check it against reality

This is the one test I could never run — **it closes M1's acceptance criterion**:

```sh
.venv/bin/python -m rainalert.cli probe "$(ls -t var/raw/*.tar.bz2 | head -1)" \
    --lat 50.1109 --lon 8.6821          # your own coordinates
```

(`probe` takes one archive, so pick the newest rather than globbing.)

Compare the output against [DWD's own radar viewer](https://www.dwd.de/DE/leistungen/radarbild_film/radarbild_film.html)
or RegenRadar for the same moment. The `+0` row should match what the map shows over your location
*now*; the later rows are the nowcast.

Do this **while it is actually raining somewhere you can see**. Agreement on a dry day proves much
less.

## 5. Warn yourself

```sh
make serve          # http://localhost:8000
```

Subscribe with any address at your own coordinates. The confirmation mail lands in `var/outbox/` as
a `.eml` — open it (double-clicking opens it in Mail.app) and click the link, or:

```sh
open var/outbox/*.eml
```

Then run `make run-ingest` twice more. If rain is approaching your location you will get a warning
`.eml`. If it is dry, temporarily lower the bar to see the machinery work:

```sh
psql rainalert -c "update subscriptions set threshold_mm_5min = 0.01, lead_time_minutes = 120;"
```

## 6. Let it run

```sh
while true; do make run-ingest; sleep 300; done
```

Five minutes, not less — one request per cycle is the politeness rule the whole ingest client is
built around (DESIGN.md §4.3). After an hour or two, `http://localhost:8000/map` has real history
to slide through, and this is also the closest thing to M2's "24 h unattended" criterion that can be
done without deploying.

## 7. What "working" looks like

```sh
psql rainalert -c "select nominal_time, status, frame_count, bytes from radar_cycles order by 1 desc limit 5;"
psql rainalert -c "select state, state_since from alert_states;"
ls var/outbox/
```

- one `radar_cycles` row per five minutes, `status = ok`, `frame_count = 25`
- no duplicate `nominal_time` values, however many times you run the job
- `var/raw/` holding archives, `var/overlays/obs/` filling one PNG per cycle

## 8. If something breaks

**`psycopg.OperationalError` / socket not found** — Homebrew's Postgres uses `/tmp` as its socket
directory; Postgres.app uses `/tmp` too but a different port. `psql -c "show unix_socket_directories"`
tells you, or use a TCP DSN: `postgresql+psycopg://user@localhost:5432/rainalert`.

**`pip install` fails on `wradlib`** — it is a test-only dependency (the golden oracle for the
decoder) and the heaviest thing here. The suite skips those tests when it is absent, so
`pip install -e .` without `[dev]` is a fine fallback if you only want to run the service.

**Apple silicon** — numpy, pyproj, Pillow and psycopg all ship arm64 wheels; nothing needs
compiling.

**Nothing in `var/outbox/`** — check `NOTIFIER=file` is actually set; the default is `console`,
which prints instead of writing.

---

## What this does and does not prove

**Does:** the decoder handles a real 25-frame archive; the politeness client talks to the real
server; the projection, the state machine, the mail and the map all work on live data.

**Does not:** that mail is deliverable to real inboxes (that needs a domain with SPF/DKIM/DMARC and
a provider), or that anything survives a week unattended. Those stay with M6.

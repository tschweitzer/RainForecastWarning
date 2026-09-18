# Running the whole thing locally (macOS or Linux)

Everything works on a laptop with no cloud account, no domain and no mail provider: archives go to a
directory, mail is written as `.eml` files you can open, and the map is served by the app itself.

**This is worth doing before any deployment**, because your machine can reach
`opendata.dwd.de` and the development environment this was written in could not. Nothing in this
repository has ever spoken to the real DWD server. The first local run is therefore a genuine test,
not a demo — watch it rather than leaving it running.

---

## 1. Prerequisites

Python 3.11+ is required. macOS ships 3.9.

Postgres is required rather than SQLite: the ingest path uses advisory locks and array columns, and
testing those on SQLite would prove nothing. If you would rather not install it,
`docker run -p 5432:5432 -e POSTGRES_PASSWORD=rainalert -e POSTGRES_USER=rainalert -e POSTGRES_DB=rainalert postgres:16`
works just as well, and skips the whole of §1.1.

**macOS:**

```sh
brew install python@3.11 postgresql@16
brew services start postgresql@16
```

**Debian/Ubuntu:**

```sh
sudo apt-get install python3.11 python3.11-venv postgresql-16
sudo systemctl start postgresql
```

```sh
git clone https://github.com/tschweitzer/RainForecastWarning
cd RainForecastWarning
make dev            # virtualenv + dependencies
```

### 1.1 One extra step on Linux, none on macOS

Both platforms authenticate local connections by *peer*: Postgres takes your operating-system
username and looks for a database role of the same name. The two packages differ in whether such a
role exists.

Homebrew runs `initdb` as you, so your login is already a superuser and `createdb rainalert` simply
works. Debian and Ubuntu run it as the `postgres` system user, so `postgres` is the only role there
is - and `createdb rainalert` fails with:

```
createdb: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed:
FATAL:  role "yourname" does not exist
```

That is not a broken install. Give yourself a role once:

```sh
sudo -u postgres createuser --createdb --login "$(whoami)"
```

Then, on either platform:

```sh
createdb rainalert  # with Docker the database already exists
```

If you would rather not own a database role, use the `postgres` account directly - `sudo -u postgres
createdb -O postgres rainalert` - and put `postgresql+psycopg://postgres@/rainalert?host=/var/run/postgresql`
in `DATABASE_URL` below, running everything with `sudo -u postgres`. The role is less trouble.

## 2. Configuration

Create `.env` in the repository root. **It is not a shell script** - it is read exactly as
written, so `$(whoami)` survives as those exact characters and Postgres tries to log you in
under that name. Write your login out, or let the shell write the line for you:

```sh
echo "DATABASE_URL=postgresql+psycopg://$(whoami)@/rainalert?host=/var/run/postgresql" >> .env
```

The rest is typed by hand:

```ini
# Your login name, spelled out. macOS - Homebrew puts the socket in /tmp:
DATABASE_URL=postgresql+psycopg://yourname@/rainalert?host=/tmp
# Debian/Ubuntu instead - a different socket directory, not a different database:
# DATABASE_URL=postgresql+psycopg://yourname@/rainalert?host=/var/run/postgresql
# Docker instead: postgresql+psycopg://rainalert:rainalert@localhost:5432/rainalert

ARCHIVE_DIR=./var/raw
OVERLAY_DIR=./var/overlays
NOTIFIER=file
MAIL_OUTBOX_DIR=./var/outbox
PUBLIC_BASE_URL=http://localhost:8000

# No basemap by default. OpenStreetMap's tile servers are volunteer-run and their usage policy
# excludes applications - they will block you, and they are right to. The map draws the radar
# over a graticule with cities marked, which is enough to read a rain field. To use a provider
# you have signed up with:
# MAP_TILE_URL=https://tiles.example.com/{z}/{x}/{y}.png?key=YOUR_KEY
# MAP_TILE_ATTRIBUTION=&copy; Example Maps
#
# On OpenStreetMap's own servers during development: their policy asks that the application be
# identifiable, attributed, and light. The first is handled - the tile layer overrides this
# site's `Referrer-Policy: no-referrer` so tiles carry the origin - and the second is the
# attribution line below, which is required and must stay. The third is on you: one browser
# looking at a map is light, an unattended reload loop is not, and maxZoom stays at 12 because
# 1 km radar has nothing to show below it. For anything public, use a provider you pay or have
# signed up with; the policy excludes applications, and a deployed service is one.
# MAP_TILE_URL=https://tile.openstreetmap.org/{z}/{x}/{y}.png
# MAP_TILE_ATTRIBUTION=&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors
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
make probe LAT=50.1109 LON=8.6821       # your own coordinates
```

That probes the newest cycle `make run-ingest` stored, and prints which one. `probe` takes a
single archive - a shell glob would hand it several and it would refuse - so to check an older
cycle name it: `make probe LAT=... LON=... ARCHIVE=var/raw/DE1200_RV2609180745.tar.bz2`.

The long form, if you would rather not go through make:

```sh
.venv/bin/python -m rainalert.cli probe "$(ls -t var/raw/*.tar.bz2 | head -1)" \
    --lat 50.1109 --lon 8.6821
```

Compare the output against [DWD's own radar viewer](https://www.dwd.de/DE/leistungen/radarbild_film/radarbild_film.html)
or RegenRadar for the same moment. The `+0` row should match what the map shows over your location
*now*; the later rows are the nowcast.

Do this **while it is actually raining somewhere you can see**. Agreement on a dry day proves much
less.

## 5. Warn yourself

```sh
make serve          # http://localhost:8000
```

### On a machine that is not the one with the browser

`make serve` binds loopback, so a remote VM will refuse the connection from outside. Reach it
through an SSH tunnel rather than opening the port:

```sh
gcloud compute ssh <vm> -- -L 8000:localhost:8000    # then browse http://localhost:8000
ssh -L 8000:localhost:8000 user@host                 # any other box
```

`make serve HOST=0.0.0.0` does bind publicly, but think before using it: this server has no TLS,
its `SECRET_KEY` is the placeholder from §2, and the subscription form is open to whoever finds
the address. Set `PUBLIC_BASE_URL` to the address you actually browse if you do - every
confirmation and unsubscribe link is built from it, and they will otherwise point at a localhost
that is not yours.

Subscribe with any address at your own coordinates. The confirmation mail lands in `var/outbox/`
as a `.eml`. **Do not read the link out of the raw file** - a `.eml` is quoted-printable, so the
token appears as `token=3D...` and wraps across a line break. Copying what `cat` shows gives a
token that is wrong twice, and the page then says the token is invalid, which is true but
misleading. Let the CLI decode it:

```sh
make outbox              # newest mail, links decoded and ready to open
make outbox N=5          # the last five
```

On a desktop a mail client decodes it for you, so `open var/outbox/*.eml` works there too.

**The subscription is not active until that link is opened.** A fresh signup is `pending`; the
dispatcher only evaluates `active` and `unhealthy` rows, so an unconfirmed one is never warned
about - and `unconfirmed_purge_hours` (24 h by default) deletes it. If the server is on another
machine, the link points at whatever `PUBLIC_BASE_URL` says, so set that to the address you
actually browse or the link will send you to your own laptop.

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

### Filling the timeline you did not run for

One `make run-ingest` captures one five-minute frame, so a timeline you have been feeding by hand
is mostly gaps - and they are drawn as gaps on purpose, because holding the previous image across
a hole would fake continuity across what might have been a radar outage.

DWD keeps a rolling ~48 h of timestamped archives (`DWD_RV_FORMAT.md` §3), so the gaps are still
fetchable:

```sh
make backfill DRY_RUN=1   # what it would fetch, how long, how much
make backfill             # everything DWD still has (~48 h), asks before it starts
make backfill HOURS=3
```

**This is the only command here that makes a burst of requests to DWD**, so it is the slowest one
on purpose: strictly one at a time, oldest first, with a jittered 1-7 s pause between each, inside
the same byte budget and behind the same circuit breaker as everything else. A full 48 h fill is
577 requests, about 300 MB and roughly 40 minutes; `HOURS=12` is 145 requests and ten minutes. It stops and says so if the budget runs out or the breaker
opens, and a cycle DWD no longer keeps is counted and skipped rather than retried.

Backfilled cycles are **never evaluated for alerts**. They are history: warning about them would
mail every subscriber about rain that stopped hours ago, once per cycle.

## 7. What "working" looks like

```sh
psql rainalert -c "select nominal_time, status, frame_count, bytes from radar_cycles order by 1 desc limit 5;"
psql rainalert -c "select state, state_since from alert_states;"
ls var/outbox/
```

- one `radar_cycles` row per five minutes, `status = ok`, `frame_count = 25`
- no duplicate `nominal_time` values, however many times you run the job
- `var/raw/` holding archives, `var/overlays/obs/` filling one PNG per cycle

## 8. Starting over

```sh
make reset-local          # subscribers, alerts, notifications, overlays, outbox
make reset-local ALL=1    # the above plus stored radar cycles and archives
make reset-local YES=1    # skip the confirmation prompt, for scripts
```

**The default keeps the radar data, and that is the point.** Subscriptions cost a click to
recreate; every stored cycle cost a request to a service DWD provides for free, and a full timeline
is 577 of them. Re-fetch only when you actually need to test ingestion itself.

So the usual loop while testing the alerting path is:

```sh
make reset-local          # forget who subscribed and what was warned
make serve                # sign yourself up again
make run-ingest           # evaluate against the cycles you already have
```

Both forms refuse to touch anything that is not a local database — a hostname in `DATABASE_URL`
aborts the command. Both also prompt before deleting; `--yes` skips that if you are scripting it.

To go all the way back to nothing:

```sh
make reset-local ALL=1
dropdb rainalert && createdb rainalert && make migrate   # or just: make migrate
rm -rf .venv && make dev                                  # rebuild the environment too
```

`.env` is never touched by any of this — your configuration survives.

## 9. If something breaks

**`Peer authentication failed for user "$(whoami)"`** — read the name in the message: `.env` is
read literally, so the shell substitution was stored verbatim. Put your real login name in it.

**`role "yourname" does not exist`** — the Debian/Ubuntu package creates only the `postgres` role,
and peer authentication looks for one named after your login. See §1.1; one `createuser` fixes it.

**`database "yourname" does not exist` from a bare `psql`** — not a fault, and nothing to do with
this project. `psql` connects to a database named after you unless told otherwise, and you have no
such database. Name one: `psql -d postgres -c ...`.

**`psycopg.OperationalError` / socket not found** — the socket directory differs per package:
Homebrew uses `/tmp`, Postgres.app uses `/tmp` on a different port, and Debian/Ubuntu use
`/var/run/postgresql`. `psql -d postgres -c "show unix_socket_directories"` tells you which, or side-step it
with a TCP DSN: `postgresql+psycopg://user@localhost:5432/rainalert`.

**`pip install` fails on `wradlib`** — it is a test-only dependency (the golden oracle for the
decoder) and the heaviest thing here. The suite skips those tests when it is absent, so
`pip install -e .` without `[dev]` is a fine fallback if you only want to run the service.

**Apple silicon** — numpy, pyproj, Pillow and psycopg all ship arm64 wheels; nothing needs
compiling.

**Nothing in `var/outbox/`** — check `NOTIFIER=file` is actually set; the default is `console`,
which prints instead of writing.

---

## 10. What this does and does not prove

**Does:** the decoder handles a real 25-frame archive; the politeness client talks to the real
server; the projection, the state machine, the mail and the map all work on live data.

**Does not:** that mail is deliverable to real inboxes (that needs a domain with SPF/DKIM/DMARC and
a provider), or that anything survives a week unattended. Those stay with M6.

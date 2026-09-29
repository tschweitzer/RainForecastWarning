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

# Nothing to set for the basemap: it defaults to basemap.de Web Raster (BKG), which needs no key
# and no account. Uncomment only to override. `make verify-basemap` confirms the endpoint with one
# request - worth doing once, because a wrong WMTS path serves blank tiles rather than an error.
#
# MAP_TILE_URL=https://tiles.example.com/{z}/{x}/{y}.png?key=YOUR_KEY
# MAP_TILE_ATTRIBUTION=&copy; Example Maps
#
# Mind the placeholder order if you do. basemap.de is WMTS and wants {z}/{y}/{x}; most
# OSM-derived providers want {z}/{x}/{y}. Swap them and you get a scrambled map, not an error.
#
# Set both to empty for no basemap at all - the radar over a graticule with cities marked. That
# was the default until 2026-09-27 and is still supported: it is the option for working offline,
# or for not having a third party in the request path at all.
# MAP_TILE_URL=
# MAP_TILE_ATTRIBUTION=
#
# On colour: a basemap with saturated green landcover fights the overlay, because `mäßiger Regen`
# is green as well. basemap.de also publishes a grey variant (swap `_farbe` for `_grau` in the
# URL) if that bothers you; the other fix is to change the intensity ramp instead. See
# "Choosing a basemap" below.
#
# Zoom goes to 18 so street names are readable. The radar overlay goes blocky past ~12, which is
# honest about it being 1 km data.
#
# What this used to point at, kept as a warning rather than an option: OpenStreetMap's own tile
# servers are volunteer-run and their usage policy excludes applications. One browser looking at a
# map during development is light; a deployed service on a five-minute cadence is an application,
# and they will block it - rightly. There is no longer any reason to reach for it, since the
# default costs nobody anything. Do not uncomment:
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

### Testing warnings without any mail at all

Web push sends the warning to your browser instead of to a file. No domain, no provider, no mail
credentials - which is why it works today while the mail questions are still open.

One thing to generate first, once:

```sh
make dev                       # if you have not already
.venv/bin/python -m rainalert.cli vapid-keys
```

Put the private key in `.env`. It is multi-line PEM, so it needs quoting - or keep it in a file and
export it:

```ini
NOTIFIER=auto
VAPID_SUBJECT=mailto:you@example.org
VAPID_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nMIGH...\n-----END PRIVATE KEY-----\n"
```

`NOTIFIER=auto` routes each message by its channel: push endpoints to the web push transport,
addresses to SMTP (D-39). Unlike the old single-transport `NOTIFIER=ntfy`, there is no way for an
email address to end up on the push transport by accident.

**Two things will stop this working on localhost, and both are the browser's rules rather than
ours.**

First, a service worker needs a secure context. `http://localhost` counts as one - Chrome and
Firefox both treat it as trustworthy - but `http://192.168.x.x` does not, so testing from your
phone against a laptop on the LAN will fail at `navigator.serviceWorker.register` with no obvious
reason. Use a tunnel with a real certificate for that, or test push in the desktop browser and the
map on the phone.

Second, a real push service has to be reachable. The notification goes out through
`fcm.googleapis.com` or Mozilla's equivalent, so a machine with no outbound HTTPS will subscribe
fine and then fail at send time with a connection error in the ingest log.

Then: open the site, leave "Push auf dieses Gerät" selected, drop a pin, and submit. The browser
asks for notification permission at that point - not on page load, deliberately (D-45) - and a test
notification arrives a moment later. Tapping it confirms the subscription. That tap replaces opening
an inbox, and if nothing arrives the warnings would not have reached you either, which is the whole
point of sending it.

**To see what is actually being sent**, `NOTIFIER=file` still writes every message to
`MAIL_OUTBOX_DIR` as a `.eml`, including the ones bound for push - the body and the action buttons
are visible there without a browser or a push service in the loop. `make outbox` prints the links
decoded.

**Testing the liveness notification** (D-46) without waiting for the schedule:

```sh
.venv/bin/python -m rainalert.cli liveness --dry-run   # who is due, and nothing sent
.venv/bin/python -m rainalert.cli liveness             # send, and delete whoever has gone
```

`--dry-run` lists the subscribers a real run would notify. Lower `WEBPUSH_LIVENESS_DAYS` to `0` in
`.env` to make everybody due.

If you would rather exercise the real mail path, point the SMTP notifier at a local catcher -
this runs the same code that will run in production, which the `.eml` file never does:

```sh
curl -sL https://github.com/axllent/mailpit/releases/latest/download/mailpit-linux-amd64.tar.gz \
  | tar xz mailpit && ./mailpit          # SMTP on 1025, web UI on 8025
```

```ini
NOTIFIER=smtp
SMTP_HOST=127.0.0.1
SMTP_PORT=1025
SMTP_USE_TLS=false
```


Then run `make run-ingest` twice more. If rain is approaching your location you will get a warning
`.eml`. If it is dry, temporarily lower the bar to see the machinery work:

```sh
psql rainalert -c "update subscriptions set threshold_mm_5min = 0.01, lead_time_minutes = 120;"
```

### Getting working links in messages on a VM

Every link in every message - confirmation, settings link, unsubscribe, the tap target on a push
warning - is built from `PUBLIC_BASE_URL`, and nothing else in the code knows a hostname. So a
message that points at a reachable address is one line, no code:

```ini
PUBLIC_BASE_URL=http://203.0.113.10:8000    # the VM's external IP, the port you serve on
```

Restart the server and every message carries that address:

```
confirmation   http://203.0.113.10:8000/confirm#a=...   (#t= in mail - D-36)
settings link  http://203.0.113.10:8000/manage#t=...
unsubscribe    http://203.0.113.10:8000/unsubscribe#t=...
warning taps   http://203.0.113.10:8000/#l=...
```

The tap target on a warning carries a **locate reference**, not coordinates: it opens the map on
the place that warning was about and stops doing so after `LOCATE_LINK_TTL_MINUTES` (60), after
which the same link opens the ordinary country view. That is deliberate - a warning sits in a
notification list for good, and last week's should not still point at where its owner lives
(DESIGN.md D-38).

**This is a development setting and it is tracked as Q-10 in DESIGN.md §19.** Over plain http,
three things are true and all of them stop being acceptable the moment anyone else subscribes:

- **Every token is submitted in clear.** All four links now carry their token in the fragment
  (D-26), which is never put on the wire at all - so the URLs themselves are safe even here.
  What is not safe is what follows: the page reads the fragment and `POST`s the token, and over
  plain http that body is readable by anyone on the path, who can then spend it - confirm
  somebody else's signup, unsubscribe them, or open their settings. The fragment fixed the
  *logging* problem (D-26, D-31); only TLS fixes this one.
- **The session cookie drops its `Secure` flag**, deliberately - a `Secure` cookie over http is
  discarded by the browser and the login would look broken. So the session travels in clear too.
- **The locate button cannot work**, because browsers refuse geolocation outside a secure
  context. That is the section below.

None of it matters while the only subscriber is you and the only traffic is your own testing.
All of it matters on the first day it is not. Setting `PUBLIC_BASE_URL` to an `https://` origin
fixes all three at once - the `Secure` flag keys off this same value.

### "Meinen Standort verwenden" does nothing over http

It is not the button. **Browsers only allow geolocation in a secure context** - https, or
`localhost`. On `http://<the VM's IP>:8000` the API is still *present*, so a
`if (!navigator.geolocation)` check passes, and the call then fails with `PERMISSION_DENIED` and
"Only secure origins are allowed". Measured in Chromium:

| Origin | `isSecureContext` | `getCurrentPosition` |
|---|---|---|
| `http://192.0.2.2:8123` | `false` | error 1, "Only secure origins are allowed" |
| `http://localhost:8123` | `true` | prompts normally |

The pages now say which of those happened instead of failing silently, but the remedy is the
origin, not the page. The SSH tunnel §5 already recommends is the fix - it makes the origin
`http://localhost:8000`, which counts as secure:

```sh
ssh -L 8000:localhost:8000 user@host    # then browse http://localhost:8000
```

`make serve HOST=0.0.0.0` and browsing the VM's address directly will never have a working
locate button, no matter what the page does.

### Changing the settings from the web page

`/manage` is the settings page: threshold, lead time, radius and location, with the location
pickable on a map. It needs no access key - reaching it always comes down to the one thing the
service can check, which is that a message sent to the channel arrived.

There are three ways in, and only the last one involves typing anything.

**Confirming.** Tapping the confirmation notification already proves the channel reaches you -
the same proof a magic link gives, a few seconds earlier - so `POST /confirm` opens the session
itself and the page that follows has a button straight into the settings. A new subscriber never
needs a link at all.

**The "Einstellungen" button on a notification** (push only). The message sent right after
confirming carries it and says to keep that message; every rain alert carries it too, because
alerts are the ones that reliably arrive again - and since D-45 they are the *only* thing that
does, because a web push notification is gone the moment it is swiped and there is no earlier one to
scroll back to. Tapping it POSTs a long-lived token to `/api/v1/manage/request` from the service
worker, and we send the ordinary magic link to the same browser. Two taps, and nothing is navigated:
the worker fetches in the background, so the token never reaches a URL bar or a history entry.

That token is durable on purpose, and it is safe to leave sitting in a notification because of what
it cannot do: it **asks** for a link, it does not admit anyone, and the link it triggers goes to the
subscriber's own channel. A forwarded screenshot of an alert is therefore not a key to somebody's
home coordinates. Taps are capped per subscriber (`MANAGE_REQUEST_LIMIT_PER_HOUR`, default 5/h) so a
copied token cannot be used to buzz its owner's phone either.

**The form on `/manage`**, for a new device or a cleared history: on email, give the address and
the link is sent. On push there is nothing to give - the endpoint is 200-odd characters the reader
has never seen - so the page reads the browser's own subscription instead and offers one button,
"Link an diesen Browser schicken". A browser with neither a subscription nor the email channel is
told plainly that it cannot be helped from there, because it cannot.

#### If the browser's site data is gone

There is no recovery, and the page says so before anyone signs up (D-47). The push subscription and
the settings cookie live in the same site-data bucket, and Chrome clears them together: "Cookies und
andere Websitedaten" unregisters the service worker, which deactivates the subscription. Nothing is
left in that browser to identify anyone with.

What happens next is automatic on both sides. The reader signs up again, which takes under a minute
and gives them a fresh row with default settings. Their old row is deleted the first time something
is sent to the dead endpoint and the push service answers 410 - within days if it rains, and
otherwise at the next weekly liveness run (D-46).


1. Open `/manage` and delete the subscription.
2. Sign up again.

That mints a topic in a browser session the other party is not in, so they never learn it, and
the old one stops being published to. What it costs you is retyping the location, threshold,
lead time and radius - which is the only thing a rotate button would have saved.

```sh
make serve                      # then open http://localhost:8000/manage
make outbox                     # the link it sent, decoded
```

The link looks like `…/manage#t=<token>`. **The token is in the fragment on purpose:** a fragment
is never sent to the server, so unlike `?token=` it cannot land in a request log or a `Referer`
header (SECURITY_REVIEW.md F-4/F-8). The page reads it, trades it for a session cookie, and
erases it from the address bar. It is good for 15 minutes and **once** - opening the same link
twice fails the second time, by design.

What you can change, and the limits:

| Field | Range | Why that range |
|---|---|---|
| Threshold | a dropdown of the map's seven intensity bands (DESIGN.md §11.1.1) | so "warn me at orange" means the same thing on the settings page and the radar map. The API still accepts 0.01 – 40.0: 0.01 is RV's own quantum (`PR E-02`), and above 40 a cycle is rejected as implausible at ingest so a higher threshold could never fire |
| Lead time | 5 – 120 min, in steps of 5 | the whole forecast RV carries; `rules.py` walks leads in fives, so 32 would be evaluated as 30 |
| Radius | 0 – 20 000 m | the `radius_sane` CHECK. Under ~500 m it is the one 1 km grid cell you stand in |

Saving the location and saving the rule are two requests, because they are two different changes:
a move resets the alert state to `UNKNOWN` (D-17) and a rule change deliberately does not. The
page says so after a move, since "saved" alone would not explain why no warning follows.

Three things that trip people up locally:

- **Requesting a link is limited to five an hour per IP** (`MANAGE_LINK_LIMIT_PER_HOUR`), same as
  signing up and for the same reason: the endpoint sends a message to an address someone typed.
  A test session spends them quickly. `make reset-local YES=1` clears the counter.
- **An unconfirmed subscription gets no link at all.** Confirmation is what proves the channel
  reaches the person; the page will not take that on trust. Confirm first.
- **The answer is the same for an address that does not exist.** That is deliberate - anything
  else would let a stranger test who has signed up - so "the link is on its way" is not a
  confirmation that the address is known.

With `MAP_TILE_URL` cleared there is no basemap, so the picker draws the radar and a few cities
over an empty background - good enough to choose a town, not a street. The default basemap (§2)
carries street names, which is what makes aiming at your own roof possible.

### Moving an existing subscription somewhere else

One subscriber has exactly one location, and it is overwritten in place rather than appended to
(D-16) - there is no location history to accumulate. Changing it is an API call, not a page:
there is no `/manage` UI in v1, so the subscribe form is for new subscriptions only and filling
it in again with different coordinates creates a second subscription instead of moving the first.

The call needs the **access key** (`Zugangsschlüssel`) that `/confirm` showed once after the
subscription was activated:

```sh
TOKEN=<the key from the confirmation page>
BASE=http://127.0.0.1:8000

# where it thinks you are now
curl -s $BASE/api/v1/subscriptions/me -H "Authorization: Bearer $TOKEN"

# move it - 204, no body
curl -s -X PUT $BASE/api/v1/subscriptions/me/location \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"lat": 53.5511, "lon": 9.9937}' -w 'HTTP %{http_code}\n'
```

`GET /api/v1/subscriptions/me` is the way to check it took: it returns the stored `lat`/`lon` and
`location_updated_at` alongside the rule values. Expect the coordinates to come back with **four
decimals** - roughly 11 m, and all a 1 km radar cell can justify keeping, so they are rounded at
the API edge rather than refused (DESIGN.md §13). The subscribe form applies the same rule.

What the move does besides changing two numbers: the cached grid cell is dropped, and the next
dispatcher run sees a `location_updated_at` newer than the state it has and resets that state to
`UNKNOWN` (D-17). That reset is the point. Without it, moving into rain that is already falling
would produce a "rain is starting" warning for rain you are already standing in.

Limits and failure modes:

- `401` - the key is wrong, or the subscription was deleted. There is no "wrong password" vs
  "no such account" distinction on purpose.
- `422` - the coordinates are not coordinates, or extra fields were sent. Only `lat` and `lon`
  are accepted.
- `429` - 60 updates an hour per IP (`LOCATION_LIMIT_PER_HOUR`). A phone reporting its position
  will not notice; a loop will.

**If the access key is lost, there is no way to get it back.** It is stored hashed and displayed
exactly once, and nothing re-issues it. That is what `/manage` is for: it asks for a fresh link
on the channel instead, so a lost key no longer means a lost subscription. The key still matters
for anything talking to the API directly - a script, or the app that does not exist yet - and for
that case, while developing, go around the API instead:

```sh
psql rainalert -c "update subscriptions set lat = 53.5511, lon = 9.9937,
  location_updated_at = now(), grid_row = null, grid_col = null;"
```

Both of the last two columns matter. `location_updated_at` is what triggers the D-17 reset, and
`grid_row`/`grid_col` cache the radar cell the old coordinates resolved to - leave them and the
subscription keeps being evaluated against where it used to be.

## 6. Let it run

```sh
while true; do make run-ingest; sleep 300; done
```

Five minutes, not less — one request per cycle is the politeness rule the whole ingest client is
built around (DESIGN.md §4.3). After an hour or two, `http://localhost:8000/` has real history
to slide through, and this is also the closest thing to M2's "24 h unattended" criterion that can be
done without deploying.

### Choosing a basemap

**The default is already a reasonable answer**, so read this only if you want to change it.
`MAP_TILE_URL` points at basemap.de Web Raster — the German federal mapping agency's (BKG) own
basemap, published under CC BY 4.0. No API key, no account, no quota, no billing relationship, no
non-commercial clause, and it is a German public body rather than an ad-funded one. Coverage is
Germany only, which sounds like a limitation and is not: the DE1200 radar composite stops at
roughly the same border, so a global basemap would only buy you a prettier view of places where
this service has nothing to say.

Confirm the endpoint once before relying on it:

```
make verify-basemap
```

A wrong WMTS path returns blank tiles rather than an error, which on a rain map looks exactly like
"no rain anywhere". That target substitutes one tile and checks for a 200 with an `image/*` type.

#### If you want to change it

The radar is the thing being read; the basemap only has to answer "where is that".

**Preview them all in one place:** <https://leaflet-extras.github.io/leaflet-providers/preview/>
lists the tile providers that work with Leaflet and renders each one live, so you can pan to
your area and compare before editing `.env`. That is the fastest way to answer this for yourself.

| Provider / style | Tile URL template | Free allowance | Commercial? |
|---|---|---|---|
| **basemap.de Farbe** (default) | `https://sgx.geodatenzentrum.de/wmts_basemapde/tile/1.0.0/de_basemapde_web_raster_farbe/default/GLOBAL_WEBMERCATOR/{z}/{y}/{x}.png` | unlimited, CC BY 4.0 | **yes** |
| basemap.de Grau | the same with `_grau` for `_farbe` | unlimited, CC BY 4.0 | **yes** |
| Esri World Light Gray | `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}` | 2M tiles/month, then metered | **yes** |
| CARTO Positron | `https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png` | 5M tiles/month, 1M if commercial | yes, reduced |
| CARTO Dark Matter | `.../dark_all/{z}/{x}/{y}{r}.png` | as above | yes, reduced |
| Stadia Alidade Smooth | `https://tiles.stadiamaps.com/tiles/alidade_smooth/{z}/{x}/{y}{r}.png` | 200k credits/month | **no** |
| MapTiler | keyed, see their console | 100k requests/month | **no** |
| Jawg | keyed, see their console | 25k map views/month | **no** |

Four things that bite when switching:

1. **Placeholder order.** WMTS services (basemap.de, Esri) want `{z}/{y}/{x}`. OSM-derived ones
   want `{z}/{x}/{y}`. Getting it backwards renders a scrambled map at a plausible zoom, with
   every tile a real tile in the wrong place — no error anywhere. `{r}` is Leaflet's retina
   placeholder and expands to `@2x` on high-density screens.
2. **"Free" often means non-commercial.** The three marked **no** above disallow commercial use on
   their free tiers, which includes the day this page starts carrying advertising. basemap.de and
   Esri have no such clause. The figures here were checked in September 2026 and this is exactly
   the sort of thing providers change, so read their current terms rather than this table.
3. **A key in `MAP_TILE_URL` is public.** It lands in the rendered HTML and in every visitor's
   network tab — unavoidable for client-side tiles, and every provider expects it. Restrict it by
   HTTP referer to your own hostname in their console, or somebody else will spend your quota.
4. **Attribution is a licence condition, not a courtesy.** Every provider above requires it, and
   every OSM-derived one also requires the OpenStreetMap credit. Paste their exact required string
   into `MAP_TILE_ATTRIBUTION` rather than paraphrasing. Terraform refuses an apply that sets a
   tile URL with an empty attribution, and `tests/test_vendored_leaflet.py` asserts the same for
   the defaults.

#### On colour

A basemap with saturated green landcover fights the overlay, because `mäßiger Regen` is green as
well. There are two fixes and the basemap is only one of them: switch to a muted style (grey
"positron"/"light"/"canvas", or basemap.de's own `_grau`), **or** change the intensity ramp in
`rainalert/radar/overlay.py` to a single blue-to-violet progression, which is what most modern
radar products do and reads better for intensity anyway. The second is the better fix if you like
a colourful map, because it stops the constraint propagating into every future provider choice.

### Choosing how far back the map looks

The slider shows the last 12 hours by default. The picker under the map changes that, and
each choice is a plain link with its own address:

```
http://localhost:8000/            12 h, the default (or whatever this browser last chose)
http://localhost:8000/?hours=3     3 h
http://localhost:8000/?hours=48   48 h, everything DWD retains
```

Any number of hours works, not only the ones offered: `?hours=5` is fine. Out of range is clamped
rather than refused, and something that is not a number at all falls back to the default instead
of showing an error page - it is a map, not a form.

Twelve is the default because 48 h is 577 slider positions, which is excellent for finding
yesterday's storm and poor for landing on a particular minute with a thumb. The ceiling is
`TIMELINE_PAST_HOURS` and the default is `TIMELINE_DEFAULT_HOURS`; the picker only offers choices
that fit inside the ceiling.

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
on purpose: strictly one at a time, oldest first, with a jittered 0.3-3 s pause between each, inside
the same byte budget and behind the same circuit breaker as everything else. A full 48 h fill is
577 requests, about 300 MB and roughly 20 minutes; `HOURS=12` is 145 requests and five
minutes. Each fetch is logged as `fetch Xs work Ys`, split deliberately: if `fetch` grew, the far
end or the network did it, and `[N attempts]` says whether we were retried; if `work` grew, this
machine did. Decoding 25 frames is real CPU, and a burstable VM (`e2-micro`, `e2-small`) runs at
full speed until its credits are gone and then clamps to a fraction of a core - which looks
exactly like a step change partway through a run. `grep -c steal /proc/stat` is not it;
`vmstat 1` and its `st` column is. It stops and says so if the budget runs out or the breaker
opens, and a cycle DWD no longer keeps is counted and skipped rather than retried.

Backfilled cycles are **never evaluated for alerts**. They are history: warning about them would
mail every subscriber about rain that stopped hours ago, once per cycle.

### Running it when you only have ssh

Anything started from an ssh session dies with that session. These targets do not: each one puts
its job in a process group of its own, records the leader's pid in `var/run/<name>.pid` and
appends output to `var/log/<name>.log`.

```sh
make serve-bg HOST=0.0.0.0     # the web service
make backfill-bg               # the long download, unattended
make ingest-loop-bg            # one ingest every 5 min - this is M2's 24 h criterion

make status                    # what is running
make logs NAME=serve           # tail -f the log
make stop NAME=ingest-loop     # stop it, and its children
```

**Changed the colour palette, the overlay opacity or `WIDTH`?** Those are baked into the rendered PNGs,
so the change only shows on cycles ingested afterwards - the map keeps showing hours of frames
drawn the old way. `make rerender` rebuilds the whole stored timeline from the archives already
on disk, without a single request to DWD:

```sh
make rerender                     # minutes, no network, no DWD traffic
make rerender LIMIT=100           # only the newest 100 archives
make rerender PAUSE=0.5           # idle between archives; slower, but the box stays usable
```

**If the whole VM becomes unresponsive part-way through**, that is CPU, not memory. The job runs
at `nice 10` so it yields to sshd and the web server, but nice only arbitrates between processes
on the machine - it cannot stop a shared-core instance being throttled once it has burned its
CPU allowance, and then *everything* on the box runs at the baseline rate. `PAUSE=0.5` keeps the
average below that baseline: roughly twice the wall-clock time, and an ssh session that still
responds. Memory is not the cause - it is flat at ~165 MB from the first archive to the last,
measured over 250 of them, and there is a test asserting it does not grow.

**If it ends in `Killed`** - `make: *** [Makefile:177: rerender] Killed` - that is the kernel's
OOM killer, not the program exiting. `make` reports the signal and nothing else, so the reason
is in the kernel log rather than anywhere the job could print it:

```sh
sudo dmesg -T | grep -i -E 'killed process|out of memory'
journalctl -k --since '10 minutes ago' | grep -i oom    # same thing, if journald is running
```

One cycle is 25 frames of 1200x1100 float32 plus a mask - 6.6 MB each, 165 MB decoded in full -
so a job that holds more than it needs runs out of room on a 1 GB machine quickly. The rerender
reads **only the first tar member**, which is where RV puts the analysis frame, so it neither
unpacks nor holds the other 24. Measured per archive: 2.25 s and a 252 MB peak reading the whole
cycle, against 0.055 s and 9 MB reading one member.

If a machine still cannot hold a run, `LIMIT` is the way down, and the newest archives are the
ones anybody is looking at.

**After a `git pull`, run `make migrate` before restarting.** A pull can bring a schema change
with it, and new code on an old schema connects perfectly well and then fails on the first
request that touches whatever the migration added - as a 500, a long way from the cause. The
whole update is:

```sh
git pull
make migrate                      # usually a no-op, and cheap when it is
make stop NAME=serve && make serve-bg HOST=0.0.0.0
```

The migration does not need the restart, and the restart does not need the migration: `alembic`
talks to the database directly, so running `make migrate` against a server that is already up
fixes it in place.

If it is ever missed, two things now say so rather than leaving it to a 500:

```sh
make logs NAME=serve              # "DATABASE SCHEMA IS OUT OF DATE: ... run `make migrate`"
curl -s localhost:8000/readyz     # 503, with the same sentence
```

`/healthz` deliberately stays 200 - the process is alive, it just should not be taking traffic.

`make stop` kills the whole process group rather than the one pid. That matters for the ingest
loop: it runs a python child per cycle, and killing only the loop would leave that child running,
reparented to init, invisible to `make status` and still talking to DWD.

Two details worth knowing. `serve-bg` runs without `--reload`, because the reloader runs the app
in a child process and the pid we record would not be the server holding the port - use plain
`make serve` while you are editing code. And if the command dies at once, the target says so and
prints the tail of the log rather than leaving a pid file that claims otherwise.

`INGEST_INTERVAL=60 make ingest-loop-bg` shortens the loop for a quick test; DWD publishes every
five minutes, so anything below 300 fetches nothing new and just re-asks.

For something that should also survive a reboot, cron is the smaller tool. Install the line, and
give it a marker comment so it can be found again later:

```sh
( crontab -l 2>/dev/null; echo '4-59/5 * * * * cd ~/RainForecastWarning && make run-ingest >> var/log/ingest.log 2>&1  # rainalert-ingest' ) | crontab -
```

`4-59/5` and not `*/5` on purpose: DWD publishes a cycle three to five minutes after its nominal
time, so a job on the exact five-minute mark asks for a file that is not there yet.

Check what is installed, and stop it again:

```sh
crontab -l                                        # what cron will run
crontab -l | grep -v rainalert-ingest | crontab -  # remove our line, keep the rest
```

The removal is a filter, not a delete: it rewrites the crontab without the marked line and leaves
every other entry alone. Running it twice is harmless. `crontab -e` opens the same file in an
editor if you would rather see what you are removing.

If the line was installed before it carried a marker, `grep -v` on the marker will not find it -
filter on `run-ingest` instead, after checking with `crontab -l` that nothing else of yours
mentions it.

Do **not** reach for `crontab -r`. It removes *every* cron job this user has, ours and yours
alike, without asking.

Removing the line stops cron from starting new runs; it does not kill a cycle that is running
right now. `make status` shows nothing for cron jobs - it only knows about the `-bg` targets -
so use `pgrep -af "rainalert.cli ingest"` to see whether one is still in flight. Each run is a
single cycle and exits on its own within a minute or so.

### "Das hat nicht geklappt" when the input was fine

Signing up is limited to five attempts an hour per IP (`SUBSCRIBE_LIMIT_PER_HOUR`), which a
testing session spends quickly. The page now says so rather than blaming the form, and the server
log shows `429 Too Many Requests`.

The counter lives in `rate_limit_hits`, which `make reset-local` truncates:

```sh
make reset-local YES=1    # also clears subscribers, alerts and the outbox; keeps radar data
```

Raising the limit is the wrong fix - it is what stops the endpoint being used to mail-bomb
someone - but it is a setting, if a long test session needs more room.

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

"""Configuration, all of it from the environment (DESIGN.md §14).

Defaults are the ones the design specifies. Anything security-relevant is commented with *why* the
default is what it is, because these are the values an operator is most likely to "tune" without
realising what they are for.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: How far back DWD's rv/ directory reaches, measured rather than assumed: the listing of
#: 2026-09-16 spanned 47 h 55 min, about 576 cycles (DWD_RV_FORMAT.md §3). Every window in this
#: file is expressed against it, so if that ever changes there is one number to edit.
#:
#: Nothing depends on it being right. A cycle DWD no longer keeps is a 404, which backfill counts
#: and skips, and a slot with no cycle is drawn as a gap rather than faked.
DWD_RETENTION_HOURS = 48


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://rainalert@localhost/rainalert"
    archive_dir: str | None = None  # local archive root; unset means GCS
    gcs_bucket: str | None = None

    # --- DWD source and politeness (§4.3) -------------------------------------------------
    dwd_base_url: str = "https://opendata.dwd.de/weather/radar/composite/rv/"
    #: The HDF5 edition (D-74). The binary `DE1200_RV_LATEST.tar.bz2` stopped at 2026-10-20 07:55.
    dwd_latest_name: str = "composite_rv_LATEST.tar"
    #: Must identify us and carry a contact address we control. Not a personal address.
    dwd_user_agent: str = "RainAlert/0.1 (+https://example.invalid; contact: ops@example.invalid)"
    dwd_max_attempts: int = 5
    dwd_backoff_base_seconds: float = 20.0
    dwd_request_timeout_seconds: float = 30.0
    #: Consecutive failed cycles before the breaker opens.
    dwd_breaker_threshold: int = 5
    dwd_breaker_cooldown_seconds: float = 900.0

    # --- Untrusted-input limits (§4.3.1) --------------------------------------------------
    #: Hard cap on one response. Content-Length is attacker-supplied, so the stream is counted too.
    dwd_max_response_bytes: int = 32 * 1024 * 1024
    #: Per-hour budget exists so exhaustion costs an hour, not a day. Halting means nobody is warned.
    dwd_hourly_byte_budget: int = 512 * 1024 * 1024
    dwd_daily_byte_budget: int = 8 * 1024 * 1024 * 1024

    # --- Cycle validation (§4.3.1 rules 5-6) ----------------------------------------------
    cycle_max_future_minutes: int = 15
    cycle_max_age_hours: int = 3
    plausibility_max_mm_5min: float = 40.0
    #: Observed band is 45-55 % (DWD_RV_FORMAT.md §8). Outside it, the composite is not normal.
    plausibility_missing_low: float = 0.35
    plausibility_missing_high: float = 0.65
    #: A complete cycle is 25 frames, t+0 ... t+120.
    expected_frame_count: int = 25

    # --- Alerting (§9) ------------------------------------------------------------------------
    #: Defaults from D-13. Per-subscription columns override these; these are what a new
    #: subscription gets.
    default_radius_m: int = 2000
    default_threshold_mm_5min: float = 0.15
    default_lead_minutes: int = 30
    dry_clear_minutes: int = 30
    warned_retract_cycles: int = 3
    missing_fraction_limit: float = 0.30
    #: What a subscriber may set the rule to, from the settings page or the API.
    #:
    #: The threshold floor is the RV product's own quantum: values are `raw * 0.01` mm per
    #: interval (DWD_RV_FORMAT.md §7, `PR E-02`), so 0.01 is the smallest difference the data can
    #: express and anything finer is a number the radar cannot answer. It is also exactly the
    #: floor of `numeric(5,2)`: 0.001 rounds to 0.00 in the column and trips the
    #: `threshold_positive` CHECK as a 500 rather than a validation error.
    #:
    #: The ceiling is `plausibility_max_mm_5min`, not a separate number, because a cycle whose
    #: peak exceeds it is rejected at ingest (jobs/ingest.py) - so a threshold above it could
    #: never fire on data this service accepts.
    min_threshold_mm_5min: float = 0.01
    #: Lead times the RV product carries: 5 ... 120 in steps of 5. rules.py steps by 5, so a
    #: lead that is not a multiple would silently round down.
    min_lead_minutes: int = 5
    max_lead_minutes: int = 120
    #: The DE1200 grid is 1 km, so anything under ~500 m samples the single cell you stand in.
    max_radius_m: int = 20000
    #: The plausibility check that replaced the blast radius (D-70). A cycle is set aside - no state
    #: changes, no notifications, as if its data were missing - when the share of the radar area that
    #: is wet "now" rises by more than this many percentage points against the previous cycle, or
    #: when any forecast frame is that much wetter than the cycle's own "now".
    #:
    #: Measured 2026-10-14 on 31 h of production frames: the largest 5-minute change was 0.69 pp and
    #: the largest forecast-vs-now difference 1.48 pp. A radar site returning from maintenance in
    #: rain adds up to ~5 pp; a corrupt "rain everywhere" cycle 85-95 pp. 20 sits far from both.
    #: Judged from the data rather than from how many subscribers react, so it means the same with
    #: five subscribers in one town as with ten thousand across the country. 0 disables it.
    implausible_rise_pp: float = 20.0
    #: The previous cycle only counts if it is this recent; after a longer gap the rise is not
    #: compared (rain can genuinely spread over half an hour), only forecast against "now".
    implausible_window_minutes: int = 15
    #: Ceiling on rain notifications (warnings and "Es regnet jetzt") one subscription receives per
    #: rolling 24 h, whatever rule it has set. SECURITY_REVIEW.md F-15: `threshold=0.01, lead=120,
    #: radius=20000` is within spec on every axis and together means "close to always" in German
    #: autumn.
    #:
    #: 24, raised from 12 (D-70): real weather gives at most one notification per rain event, and a
    #: new event needs 30 dry minutes first, so 24 is out of reach of any real day - the busiest
    #: subscription measured had 7. What is left for it to catch is a bug or an absurd rule, and
    #: when it does, it says so in the log. 0 disables it.
    alert_cap_per_subscription_per_day: int = 24
    #: How many push notifications are sent at once (D-70). Each one is a separate HTTPS request
    #: that spends ~100 ms waiting on the push service; one after another, 3,000 of them would take
    #: about five minutes and outrun the job. Push services are built for concurrent senders.
    delivery_concurrency: int = 32

    # --- Map timeline (D-22, §11.1) -----------------------------------------------------------
    #: The furthest back the map will go for anyone who asks - the whole window DWD keeps.
    #: Anything more is slots that can never be filled.
    timeline_past_hours: int = DWD_RETENTION_HOURS
    #: What the map shows when nobody asked for anything. Twelve hours is a slider you can aim:
    #: 145 positions rather than 577, and it covers "did it rain while I was asleep". The rest
    #: is a click away and the URL is shareable.
    timeline_default_hours: int = 12
    #: Beyond this the page shows a "radar data is stale" banner instead of pretending.
    timeline_stale_after_minutes: int = 20
    overlay_dir: str | None = None
    #: Production: a separate bucket from the archives, and the public base URL it is served on.
    overlay_bucket: str | None = None
    overlay_public_base_url: str | None = None
    #: Two hours past the timeline, so the oldest frame on the slider is never a 404 that
    #: appeared because a prune ran while someone was looking at it.
    overlay_obs_retention_hours: int = DWD_RETENTION_HOURS + 2
    overlay_fc_retention_hours: int = 1

    # --- Retention (D-7, D-23) --------------------------------------------------------------
    #: Also past the timeline: re-rendering the oldest frame from raw has to stay possible, and
    #: keeping less than DWD does would leave a window where they still have a cycle we have
    #: discarded and would have to re-fetch.
    raw_retention_hours: int = DWD_RETENTION_HOURS + 2
    evaluation_retention_hours: int = 48

    # --- Public identity (Q-1: the domain is not chosen yet) ---------------------------------
    #: Every link in every push message and page is built from this - and nothing else in the
    #: code knows a hostname, so pointing it at a bare IP over http is all that development on
    #: a VM needs: `PUBLIC_BASE_URL=http://203.0.113.10:8000`.
    #:
    #: **It must be https before anyone but the author subscribes (Q-10).** Three things hang
    #: off the scheme, not just the links: the confirm and unsubscribe tokens ride in a query
    #: string and are readable on the wire; the session cookie drops `Secure` (see
    #: api/app.py set_session_cookie, which keys off this value) so it travels in clear; and
    #: browsers refuse geolocation outside a secure context, so the locate button cannot work.
    public_base_url: str = "http://localhost:8000"

    # --- Basemap (Q-5, resolved 2026-09-27: basemap.de) ---------------------------------------
    #: Leaflet tile template. Defaults to basemap.de Web Raster, the German federal mapping
    #: agency's (BKG) own basemap: CC BY 4.0, no API key, no account, no quota, and no
    #: non-commercial clause - so it stays valid if this service ever carries ads or takes money.
    #: Its coverage is Germany only, which is the right shape here because the DE1200 radar
    #: composite stops at roughly the same border (DESIGN.md D-43).
    #:
    #: It is a WMTS service, so the template is **{z}/{y}/{x}** - y before x, unlike the
    #: {z}/{x}/{y} that OSM-style providers use. Getting that backwards renders a scrambled map
    #: rather than an error, which is why it is called out here and asserted in the tests.
    #: `GLOBAL_WEBMERCATOR` is the tile matrix set that lines up with Leaflet's default CRS; the
    #: service also offers `DE_EPSG_25832_ADV`, which is UTM32 and will not.
    #:
    #: Set both to "" for no basemap at all: the map then draws the radar over a graticule with a
    #: dozen cities marked, which is enough to read a rain field. That was the default until
    #: 2026-09-27 and is still a supported state, not a broken one.
    #:
    #: Any other provider you have signed up with:
    #:     MAP_TILE_URL=https://tiles.example.com/{z}/{x}/{y}.png?key=YOUR_KEY
    #:     MAP_TILE_ATTRIBUTION=&copy; Example Maps
    #:
    #: Two things to know before switching. A key in this URL is public - it is in the rendered
    #: HTML and in every visitor's network tab - so restrict it by HTTP referer in the provider's
    #: console or somebody else will spend your quota. And several providers' free tiers are
    #: non-commercial only (Stadia, Jawg, MapTiler at the time of writing), which stops being
    #: allowed the day this service carries advertising; basemap.de and Esri do not have that
    #: clause. docs/LOCAL.md §"Choosing a basemap" has the comparison.
    map_tile_url: str = (
        "https://sgx.geodatenzentrum.de/wmts_basemapde/tile/1.0.0"
        "/de_basemapde_web_raster_farbe/default/GLOBAL_WEBMERCATOR/{z}/{y}/{x}.png"
    )
    #: Required by every provider worth using, and by their licence. Shown in the map's corner.
    #: CC BY 4.0 wants the licence named *and linked*, and BKG asks that its own name link to
    #: bkg.bund.de - hence the two anchors rather than a plain string. Change this whenever you
    #: change `map_tile_url`; an attribution that credits the wrong service is worse than none.
    map_tile_attribution: str = (
        '&copy; <a href="https://www.bkg.bund.de">BKG</a> (basemap.de) '
        '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>'
    )
    #: Vector tiles for the maps on the start and settings pages (DESIGN.md D-58, D-59): Shortbread
    #: tiles, which is the schema the styles in static/map/ are built for. By default the
    #: OpenStreetMap Foundation's own server, under its vector tile usage policy - best effort, no
    #: SLA, may block heavy users without notice. Empty turns the vector map off: both pages then
    #: draw with Leaflet and `map_tile_url`.
    vector_tile_url: str = "https://vector.openstreetmap.org/shortbread_v1/{z}/{x}/{y}.mvt"
    #: A contact address in every page's footer; empty shows none. The OSMF's vector tile policy
    #: recommends one, so they can reach the site's operator rather than block it. Published to
    #: everyone, so it should be an address meant for that.
    contact_email: str = ""
    #: Who is responsible for the data, as the privacy page names them (GDPR Art. 13(1)(a)): name
    #: and postal address, e.g. "Erika Muster, Musterweg 1, 12345 Musterstadt". Configuration, not
    #: code, because it is the operator's and does not belong in a repository. Empty shows "Der
    #: Betreiber dieser Website", which is not enough for a public site.
    privacy_controller: str = ""

    # --- Delivery ----------------------------------------------------------------------------
    #: console | file | webpush | push. `webpush` is production. `console` and `file` are sinks
    #: for development that take every message, so a local run never posts to a real push service
    #: unless asked to. Push only since D-76; there is no mail transport.
    notifier: str = "console"
    #: Where `NOTIFIER=file` writes each message, one text file per message.
    outbox_dir: str | None = None

    # --- Push (W3C Web Push, D-45) -------------------------------------------------------------
    #: The VAPID private key, PEM. Generate one with `rainalert vapid-keys` and keep it in Secret
    #: Manager next to `SECRET_KEY`.
    #:
    #: **Rotating it unsubscribes everybody, silently.** A push service checks the signature
    #: against the key the subscription was created with, so after a rotation every send is
    #: rejected as unauthorised - and the rejection is a 401/403, not the 410 that would tell us
    #: to delete the row. Subscribers keep their notification permission and simply stop being
    #: warned, with nothing on either side saying why. Treat it as permanent.
    vapid_private_key: str = ""
    #: The contact RFC 8292 puts in the JWT `sub` claim. Google, Apple and Mozilla receive it on
    #: every send and use it to reach the operator when something is wrong with our traffic - the
    #: same role `dwd_user_agent` plays for DWD. A role address, not a personal one: it is handed
    #: to three third parties several times a day. Must be `mailto:` or `https:`.
    #: Empty by default rather than a plausible-looking placeholder. `mailto:ops@example.invalid`
    #: was the default, and a deployment that missed the env var would have told three push services
    #: to reach the operator at an address that does not exist - which is the address they use before
    #: they start refusing traffic. Empty makes the notifier refuse to build, `create_app` logs it and
    #: disables push, and the signup page says push is unavailable. Loud beats plausible.
    vapid_subject: str = ""
    #: How long a push service should hold a message for a device that is offline, for messages that
    #: do not set their own. Rain notifications do (D-70): a warning stays valid until the rain it
    #: announces is due (`dispatcher.notification_lifetime`), so this is now the default for the rest.
    webpush_ttl_seconds: int = 1800
    webpush_timeout_seconds: float = 10.0
    #: Days of silence before the liveness notification goes out (D-46). Its real job is deletion:
    #: a subscriber who cleared their browser data never told us, and only a send attempt learns
    #: it - so this is also the longest we can hold a location for somebody who has gone.
    webpush_liveness_days: int = 30

    # --- Tokens and consent -------------------------------------------------------------------
    #: Salts the IP hashes and signs anything that needs signing. Must be set in production.
    secret_key: str = "dev-secret-change-me"
    confirm_token_ttl_hours: int = 24
    #: Which wording of the consent text was agreed to, so the record still means something after
    #: the text is edited (GDPR Art. 7(1)). **Bump this whenever the consent text changes**, or
    #: every existing record points at wording that no longer exists.
    #:
    #: One wording since D-76, the push one. Records made under 2026-09-21 by an email subscriber
    #: named an email address; there are none left (migration e5b2c8d4f1a6).
    #:
    #: 2026-09-21: split by channel, and the signup note stopped claiming that nothing is stored
    #: before confirmation. A pending row exists from the moment of signup and is deleted after
    #: `unconfirmed_purge_hours`, which is what the privacy page always said.
    consent_text_version: str = "2026-09-21"
    #: Unconfirmed subscriptions are deleted after this long (data minimisation, §13).
    unconfirmed_purge_hours: int = 24

    # --- Rate limiting (§10) ------------------------------------------------------------------
    #: How many proxy hops in front of us are ours. X-Forwarded-For is appended to by each hop, so
    #: only the last N entries are trustworthy; 0 means take the socket peer and ignore the header
    #: entirely. Getting this wrong lets a client spoof its own identity (SECURITY_REVIEW.md F-5).
    trusted_proxy_hops: int = 0
    subscribe_limit_per_hour: int = 5
    location_limit_per_hour: int = 60
    #: Writes to the rule (threshold, lead time, radius) from the settings page.
    settings_limit_per_hour: int = 60
    #: Settings-link requests. As tight as signing up: the endpoint takes a push endpoint and
    #: sends a message to it.
    manage_link_limit_per_hour: int = 5
    rate_limit_retention_days: int = 7

    # --- Self-service settings (§11.2; on the start page since D-67) ----------------------------
    #: How long a magic link works. Short, because it is a bearer credential to someone's home
    #: coordinates sitting in their inbox; single use on top of that (tokens.py).
    manage_link_ttl_minutes: int = 15
    #: How long the session it opens lasts. Long enough to pick a spot on a map and think about
    #: it, short enough that a borrowed phone is not an open account.
    manage_session_ttl_minutes: int = 30
    #: The wall a session may not be renewed past, measured from the moment the link was spent.
    #: Without it the renew button would quietly turn the line above into a formality - which is
    #: the whole protection: a session that ends at a predictable time whatever the holder does.
    manage_session_max_minutes: int = 120
    #: Device keys (DESIGN.md D-64, docs/PLAN_DEVICE_KEY.md): a push subscriber's browser signs
    #: its settings requests with a key registered alongside a push-delivered link, so the settings
    #: page opens without a session or a push round trip. The kill switch: off, no key is
    #: registered or accepted, every page falls back to the link and the cookie session, and
    #: nothing is lost. Turned off because of a verification bug, delete `device_keys` before
    #: turning it back on (RUNBOOK).
    device_key_login_enabled: bool = True
    #: Failed device-key signatures per IP per hour before the answer becomes 429. Only failures
    #: are counted; a signed request that verifies costs nothing here.
    device_key_failure_limit_per_hour: int = 60
    #: How long a warning's link keeps showing the place the warning was about. A warning is
    #: about the next two hours at most, so an hour covers looking at it while it matters and
    #: little else; after that the map opens on the country like any other visit.
    locate_link_ttl_minutes: int = 60

    #: Bearer token guarding /metrics. Unset means the endpoint does not exist at all - "internal
    #: only" is not expressible on Cloud Run, where every route is reachable from the internet
    #: unless something in the request says otherwise (SECURITY_REVIEW.md F-13).
    metrics_token: str | None = None

    log_level: str = "INFO"
    metrics_path: str | None = Field(default=None, description="write Prometheus text here on exit")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

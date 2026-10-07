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
    dwd_latest_name: str = "DE1200_RV_LATEST.tar.bz2"
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
    #: Absolute cap on how many subscriptions one cycle may warn. A cycle that would warn more is
    #: far likelier to be broken than to be a nationwide squall, and mailing everyone also burns
    #: the day's sending quota so the genuine alerts later never arrive.
    blast_radius_max: int = 25
    #: Hard ceiling on alerts one subscription may receive per rolling 24 h, whatever rule it has
    #: set. SECURITY_REVIEW.md F-15: `threshold=0.01, lead=120, radius=20000` is within spec on
    #: every axis and together means "close to always" in German autumn, and what it spends - a
    #: mail provider's daily quota, a sending domain's reputation, one VAPID key's standing with
    #: three push services - belongs to every other subscriber too.
    #:
    #: 12 is two an hour for six hours: more than a real day of weather needs, far less than a
    #: pathological rule produces. 0 disables it.
    alert_cap_per_subscription_per_day: int = 12
    #: Ceiling on alerts across *all* subscriptions per rolling 24 h (F-2's "per-run global mail
    #: ceiling", widened to a day because the quota it protects is a daily one).
    #:
    #: 300 is the mail provider free tier §6.2 budgets for. Deliberately far above
    #: `alert_cap_per_subscription_per_day` times any plausible subscriber count, because unlike the
    #: per-subscription cap this one is *shared fate*: once it is reached nobody is warned, which is
    #: the service failing at its one job. It is the backstop for the case the per-subscription cap
    #: cannot see - many accounts, each individually reasonable - and it is loud rather than quiet
    #: (see `evaluate_cycle`). 0 disables it.
    global_alert_cap_per_day: int = 300

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
    #: Every link in every email, push and page is built from this - and nothing else in the
    #: code knows a hostname, so pointing it at a bare IP over http is all that development on
    #: a VM needs: `PUBLIC_BASE_URL=http://203.0.113.10:8000`.
    #:
    #: **It must be https before anyone but the author subscribes (Q-10).** Three things hang
    #: off the scheme, not just the links: the confirm and unsubscribe tokens ride in a query
    #: string and are readable on the wire; the session cookie drops `Secure` (see
    #: api/app.py set_session_cookie, which keys off this value) so it travels in clear; and
    #: browsers refuse geolocation outside a secure context, so the locate button cannot work.
    public_base_url: str = "http://localhost:8000"
    #: Likewise a placeholder. Deliverability needs SPF, DKIM and DMARC on whatever domain this
    #: ends up on - without them these mails land in spam and the service is pointless (§12).
    mail_from: str = "RainAlert <rainalert@localhost>"
    mail_reply_to: str | None = None

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

    # --- Delivery (Q-4: the provider is not chosen yet) ---------------------------------------
    #: console | file | smtp | webpush | push | auto. SMTP reaches every provider worth using, so
    #: choosing one is a matter of credentials rather than code. `auto` is the production shape:
    #: each channel on its own transport (notify/routing.py). `console` and `file` stay sinks
    #: that take everything, so a local run never posts to a real push service.
    notifier: str = "console"
    #: Whether the email channel is offered at all. False is for a deployment that has push
    #: working and no mail provider yet: the signup page then shows push only, and the API
    #: refuses `channel=email` rather than accepting an address it has no way to write to.
    #: It gates *signing up*, not delivery - anyone already subscribed by email keeps working.
    email_channel_enabled: bool = True
    mail_outbox_dir: str | None = None
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_timeout_seconds: float = 20.0

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
    #: How long a push service should hold a message for a device that is offline.
    #:
    #: 30 minutes, matching `dispatcher.MAX_NOTIFICATION_AGE`. It was an hour, which contradicted it:
    #: the dispatcher refuses to *send* a warning older than 30 minutes because "a late rain warning
    #: is worse than none", and then the TTL told the push service to hold it for twice that. A phone
    #: off-network for 50 minutes got a warning about rain that had already come and gone - the exact
    #: case the shorter rule exists to prevent, arriving through the longer one.
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
    #: There are two wordings per version - one naming an email address, one naming a push topic -
    #: because they describe different data. The subscriber's `channel` is stored alongside, so
    #: channel plus version identifies exactly what was on screen without a second column.
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
    #: Magic-link requests. Deliberately as tight as signing up: the endpoint takes an address
    #: and sends mail to it, so it is the same mail-bomb lever as POST /subscriptions.
    manage_link_limit_per_hour: int = 5
    #: Taps on the "Einstellungen" button in a notification, counted per subscriber rather than
    #: per IP. The token that button carries is durable and travels in every alert, so the cap
    #: is what stops a copy of one being used to buzz its owner's phone indefinitely.
    manage_request_limit_per_hour: int = 5
    rate_limit_retention_days: int = 7

    # --- Self-service settings page (§11.2) ----------------------------------------------------
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
    #: How long the notification button keeps working. Long, because the message it rides in is
    #: one the reader is asked to keep; harmless, because the button only asks for a link that
    #: is itself short-lived and goes to the subscriber's own channel (tokens.py).
    manage_request_ttl_days: int = 365
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

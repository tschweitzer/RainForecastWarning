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
    #: Every link in every email is built from this. The placeholder works for local development;
    #: set it to the real origin at deploy time. Nothing else in the code knows a hostname.
    public_base_url: str = "http://localhost:8000"
    #: Likewise a placeholder. Deliverability needs SPF, DKIM and DMARC on whatever domain this
    #: ends up on - without them these mails land in spam and the service is pointless (§12).
    mail_from: str = "RainAlert <rainalert@localhost>"
    mail_reply_to: str | None = None

    # --- Basemap (Q-10: the tile provider is not chosen yet) ----------------------------------
    #: Leaflet tile template, e.g. "https://tiles.example.com/{z}/{x}/{y}.png?key=...".
    #: Empty by default, and deliberately so: the obvious choice, tile.openstreetmap.org, is a
    #: volunteer-run service whose usage policy excludes applications, and it blocks them. Using
    #: it would be taking something that was not offered. With no provider set the map draws the
    #: radar over a plain background with a graticule and a few cities for orientation, which is
    #: enough to read a rain field and costs nobody anything.
    map_tile_url: str = ""
    #: Required by every provider worth using, and by their licence. Shown in the map's corner.
    map_tile_attribution: str = ""

    # --- Delivery (Q-4: the provider is not chosen yet) ---------------------------------------
    #: console | file | smtp | push. SMTP reaches every provider worth using, so choosing one is
    #: a matter of credentials rather than code.
    notifier: str = "console"
    mail_outbox_dir: str | None = None
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_timeout_seconds: float = 20.0

    # --- Push (ntfy) --------------------------------------------------------------------------
    #: The ntfy server to publish to. The public one sees the message text and the topic name,
    #: which for a service whose privacy story is data minimisation is worth thinking about - a
    #: rain warning names a place and a time. Self-host it for anything but testing.
    ntfy_server: str = "https://ntfy.sh"
    #: Prefix for generated topics. Only the random half is what makes a topic unguessable; this
    #: is so a subscriber can recognise which of their subscriptions a topic belongs to.
    ntfy_topic_prefix: str = "rainalert"
    ntfy_timeout_seconds: float = 10.0
    #: Optional bearer token, for a self-hosted server with access control.
    ntfy_token: str | None = None

    # --- Tokens and consent -------------------------------------------------------------------
    #: Salts the IP hashes and signs anything that needs signing. Must be set in production.
    secret_key: str = "dev-secret-change-me"
    confirm_token_ttl_hours: int = 24
    #: Which wording of the consent text was agreed to, so the record still means something after
    #: the text is edited (GDPR Art. 7(1)).
    consent_text_version: str = "2026-09-16"
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
    rate_limit_retention_days: int = 7

    # --- Self-service settings page (§11.2) ----------------------------------------------------
    #: How long a magic link works. Short, because it is a bearer credential to someone's home
    #: coordinates sitting in their inbox; single use on top of that (tokens.py).
    manage_link_ttl_minutes: int = 15
    #: How long the session it opens lasts. Long enough to pick a spot on a map and think about
    #: it, short enough that a borrowed phone is not an open account.
    manage_session_ttl_minutes: int = 30

    #: Bearer token guarding /metrics. Unset means the endpoint does not exist at all - "internal
    #: only" is not expressible on Cloud Run, where every route is reachable from the internet
    #: unless something in the request says otherwise (SECURITY_REVIEW.md F-13).
    metrics_token: str | None = None

    log_level: str = "INFO"
    metrics_path: str | None = Field(default=None, description="write Prometheus text here on exit")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

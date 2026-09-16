"""Configuration, all of it from the environment (DESIGN.md §14).

Defaults are the ones the design specifies. Anything security-relevant is commented with *why* the
default is what it is, because these are the values an operator is most likely to "tune" without
realising what they are for.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Retention (D-7) -------------------------------------------------------------------
    raw_retention_hours: int = 48

    # --- Public identity (Q-1: the domain is not chosen yet) ---------------------------------
    #: Every link in every email is built from this. The placeholder works for local development;
    #: set it to the real origin at deploy time. Nothing else in the code knows a hostname.
    public_base_url: str = "http://localhost:8000"
    #: Likewise a placeholder. Deliverability needs SPF, DKIM and DMARC on whatever domain this
    #: ends up on - without them these mails land in spam and the service is pointless (§12).
    mail_from: str = "RainAlert <rainalert@localhost>"
    mail_reply_to: str | None = None

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
    rate_limit_retention_days: int = 7

    log_level: str = "INFO"
    metrics_path: str | None = Field(default=None, description="write Prometheus text here on exit")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

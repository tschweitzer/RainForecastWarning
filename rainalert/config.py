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

    log_level: str = "INFO"
    metrics_path: str | None = Field(default=None, description="write Prometheus text here on exit")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

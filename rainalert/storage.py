"""Where raw archives are kept.

Local filesystem for development; GCS in production. The GCS import is lazy so the ingest job does
not carry the google-cloud-storage dependency when running locally.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol


class ArchiveStore(Protocol):
    def put(self, nominal_time: datetime, blob: bytes) -> str:
        """Store the raw archive and return a URI for it."""

    def prune(self, older_than: datetime) -> int:
        """Delete archives older than the cutoff; return how many were removed."""


def _name(nominal_time: datetime) -> str:
    return f"DE1200_RV{nominal_time:%y%m%d%H%M}.tar.bz2"


class LocalArchiveStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, nominal_time: datetime, blob: bytes) -> str:
        path = self.root / _name(nominal_time)
        path.write_bytes(blob)
        return path.as_uri()

    def prune(self, older_than: datetime) -> int:
        cutoff = _name(older_than)
        removed = 0
        for path in self.root.glob("DE1200_RV*.tar.bz2"):
            if path.name < cutoff:
                path.unlink()
                removed += 1
        return removed


class OverlayStore(Protocol):
    def put_observed(self, nominal_time: datetime, png: bytes) -> str: ...

    def put_forecast(self, nominal_time: datetime, lead_minutes: int, png: bytes) -> str: ...

    def url_for_observed(self, nominal_time: datetime) -> str: ...

    def url_for_forecast(self, nominal_time: datetime, lead_minutes: int) -> str: ...

    def prune(self, observed_before: datetime, forecast_before: datetime) -> int: ...


def _obs_name(nominal_time: datetime) -> str:
    return f"obs/{nominal_time:%Y%m%dT%H%M}.png"


def _fc_name(nominal_time: datetime, lead_minutes: int) -> str:
    return f"fc/{nominal_time:%Y%m%dT%H%M}/{lead_minutes:03d}.png"


class LocalOverlayStore:
    """Development store. Served by the API itself from /overlays/... .

    Observed and forecast frames live under different prefixes because they have different
    lifetimes and different audiences - the 12 h timeline needs every past analysis frame, while
    only the newest cycle's forecast is ever shown (D-7).
    """

    def __init__(self, root: str | Path, base_url: str = "/overlays") -> None:
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")
        (self.root / "obs").mkdir(parents=True, exist_ok=True)
        (self.root / "fc").mkdir(parents=True, exist_ok=True)

    def _write(self, name: str, png: bytes) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        return f"{self.base_url}/{name}"

    def put_observed(self, nominal_time: datetime, png: bytes) -> str:
        return self._write(_obs_name(nominal_time), png)

    def put_forecast(self, nominal_time: datetime, lead_minutes: int, png: bytes) -> str:
        return self._write(_fc_name(nominal_time, lead_minutes), png)

    def url_for_observed(self, nominal_time: datetime) -> str:
        return f"{self.base_url}/{_obs_name(nominal_time)}"

    def url_for_forecast(self, nominal_time: datetime, lead_minutes: int) -> str:
        return f"{self.base_url}/{_fc_name(nominal_time, lead_minutes)}"

    def prune(self, observed_before: datetime, forecast_before: datetime) -> int:
        removed = 0
        for path in (self.root / "obs").glob("*.png"):
            if path.stem < f"{observed_before:%Y%m%dT%H%M}":
                path.unlink()
                removed += 1
        for directory in (self.root / "fc").iterdir():
            if directory.is_dir() and directory.name < f"{forecast_before:%Y%m%dT%H%M}":
                for path in directory.glob("*.png"):
                    path.unlink()
                    removed += 1
                directory.rmdir()
        return removed


class GCSArchiveStore:
    """Production store. Retention is a bucket lifecycle rule (D-7), so prune() is a no-op.

    Note the bucket holds raw archives *and* rendered overlays. They have different audiences -
    overlays are served to browsers, raw archives are not - so they must not share a public prefix
    (SECURITY_REVIEW.md F-9).
    """

    def __init__(self, bucket: str, prefix: str = "raw/") -> None:
        from google.cloud import storage  # lazy: not needed for local runs

        self._bucket = storage.Client().bucket(bucket)
        self._prefix = prefix

    def put(self, nominal_time: datetime, blob: bytes) -> str:
        name = f"{self._prefix}{_name(nominal_time)}"
        self._bucket.blob(name).upload_from_string(blob, content_type="application/x-bzip2")
        return f"gs://{self._bucket.name}/{name}"

    def prune(self, older_than: datetime) -> int:
        return 0  # lifecycle rule owns this

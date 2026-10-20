"""Where raw archives are kept.

Local filesystem for development; GCS in production. The GCS import is lazy so the ingest job does
not carry the google-cloud-storage dependency when running locally.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class ArchiveStore(Protocol):
    def put(self, nominal_time: datetime, blob: bytes) -> str:
        """Store the raw archive and return a URI for it."""

    def prune(self, older_than: datetime) -> int:
        """Delete archives older than the cutoff; return how many were removed."""


def _name(nominal_time: datetime, blob: bytes = b"") -> str:
    """DWD's own file name for the cycle, in the edition the bytes are in (D-74).

    Named after the content rather than after today's default, because archives from before the
    switch are still being read back (re-rendering, backfill) and a `.tar.bz2` that holds a plain
    tar - or the other way round - is a misleading thing to leave in a bucket.
    """
    if blob[:3] == b"BZh":
        return f"DE1200_RV{nominal_time:%y%m%d%H%M}.tar.bz2"
    return f"composite_rv_{nominal_time:%Y%m%d_%H%M}.tar"


def _content_type(blob: bytes) -> str:
    return "application/x-bzip2" if blob[:3] == b"BZh" else "application/x-tar"


#: Both editions' names, with the nominal time in them.
_ARCHIVE_NAMES = (
    (re.compile(r"DE1200_RV(\d{10})\.tar\.bz2$"), "%y%m%d%H%M"),
    (re.compile(r"composite_rv_(\d{8}_\d{4})\.tar$"), "%Y%m%d_%H%M"),
)


def _is_archive_name(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern, _ in _ARCHIVE_NAMES)


def archive_time(name: str) -> datetime | None:
    """The nominal time in an archive's file name, for either edition; None if there is none."""
    for pattern, layout in _ARCHIVE_NAMES:
        match = pattern.fullmatch(name)
        if match:
            try:
                return datetime.strptime(match.group(1), layout).replace(tzinfo=UTC)
            except ValueError:
                return None  # shaped like one, but not a real time - e.g. minute 99
    return None


def archive_paths(root: str | Path) -> list[Path]:
    """Every archive under ``root``, newest first, whichever edition it is in.

    A file named like an archive but with an impossible time is kept, at the end: the caller
    tries to read it and reports it, rather than this quietly deciding it does not exist.
    """
    oldest = datetime.min.replace(tzinfo=UTC)
    found = [p for p in Path(root).iterdir() if p.is_file() and _is_archive_name(p.name)]
    return sorted(found, key=lambda p: (archive_time(p.name) or oldest, p.name), reverse=True)


class LocalArchiveStore:
    """Development store, on the filesystem.

    The root is resolved to an absolute path on the way in. ``ARCHIVE_DIR`` is normally written
    relative (``./var/raw``), ``Path.as_uri`` refuses a relative path, and a process that changes
    directory would otherwise start writing somewhere else entirely.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, nominal_time: datetime, blob: bytes) -> str:
        path = self.root / _name(nominal_time, blob)
        path.write_bytes(blob)
        return path.as_uri()

    def prune(self, older_than: datetime) -> int:
        removed = 0
        for path in archive_paths(self.root):
            stamp = archive_time(path.name)
            if stamp is not None and stamp < older_than:
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
        self.root = Path(root).resolve()  # as above: relative roots move with the process
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
        name = f"{self._prefix}{_name(nominal_time, blob)}"
        self._bucket.blob(name).upload_from_string(blob, content_type=_content_type(blob))
        return f"gs://{self._bucket.name}/{name}"

    def prune(self, older_than: datetime) -> int:
        return 0  # lifecycle rule owns this


class GCSOverlayStore:
    """Production overlay store.

    Deliberately a *different bucket* from the archives, not just a different prefix. The overlays
    are served to browsers and the raw DWD archives are not; putting them side by side is how a
    "make the overlays public" step ends up publishing everything next to them
    (SECURITY_REVIEW.md F-9). Retention is a lifecycle rule per prefix, so prune() is a no-op.
    """

    def __init__(self, bucket: str, public_base_url: str) -> None:
        from google.cloud import storage  # lazy: local runs need no cloud SDK

        self._bucket = storage.Client().bucket(bucket)
        self._base = public_base_url.rstrip("/")

    #: An observation frame is immutable *and* still wanted hours later: the timeline reaches
    #: `timeline_past_hours` back, so somebody scrubbing 30 hours of history wants frames they may
    #: have loaded this morning. At `max-age=3600` they were re-fetched after an hour, which is why
    #: scrubbing a long range felt slow - the browser had them on disk and threw them away. Two days
    #: matches `overlay_obs_retention_hours` (50), and a cached copy of a frame we have since pruned
    #: is harmless: it is a historical picture that can never change.
    OBSERVED_CACHE_CONTROL = "public, max-age=172800, immutable"
    #: Forecast frames are immutable by URL too - the name carries the cycle and the lead - but only
    #: the newest cycle's forecast is ever shown (D-7) and they are pruned after an hour. Telling a
    #: browser to keep them for two days would just be somebody else's disk.
    FORECAST_CACHE_CONTROL = "public, max-age=3600, immutable"

    def _upload(self, name: str, png: bytes, cache_control: str) -> str:
        blob = self._bucket.blob(name)
        blob.cache_control = cache_control
        blob.upload_from_string(png, content_type="image/png")
        return f"{self._base}/{name}"

    def put_observed(self, nominal_time: datetime, png: bytes) -> str:
        return self._upload(_obs_name(nominal_time), png, self.OBSERVED_CACHE_CONTROL)

    def put_forecast(self, nominal_time: datetime, lead_minutes: int, png: bytes) -> str:
        return self._upload(_fc_name(nominal_time, lead_minutes), png, self.FORECAST_CACHE_CONTROL)

    def url_for_observed(self, nominal_time: datetime) -> str:
        return f"{self._base}/{_obs_name(nominal_time)}"

    def url_for_forecast(self, nominal_time: datetime, lead_minutes: int) -> str:
        return f"{self._base}/{_fc_name(nominal_time, lead_minutes)}"

    def prune(self, observed_before: datetime, forecast_before: datetime) -> int:
        return 0  # lifecycle rules own this

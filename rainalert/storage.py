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

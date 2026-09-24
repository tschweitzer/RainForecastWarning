# One image, several entrypoints: the API service, the ingest job, and the maintenance commands.
# Keeping them in one image means the decoder that alerting depends on is byte-identical to the one
# the map renders from - a split image is a way to ship two different versions of the same bug.
#
# NOTE FOR FIRST BUILD: pin this by digest (SECURITY_REVIEW.md F-18). `make pin-base` prints the
# line to paste. A tag is mutable; a digest is what makes the build reproducible.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# pyproj needs PROJ's data files; everything else here is wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the whole tree.
#
# With the `gcs` extra: this image is the production one, and production stores overlays and
# archives in GCS. The adapters import google-cloud-storage lazily, so a plain `pip install .`
# builds and starts fine right up until OVERLAY_BUCKET is set - at which point `create_app`
# constructs GCSOverlayStore, the lazy import raises, and Cloud Run reports only that the
# container failed its startup probe.
COPY pyproject.toml ./
RUN pip install --no-cache-dir '.[gcs]'

COPY rainalert ./rainalert
COPY migrations ./migrations
COPY alembic.ini ./

# Runs as a non-root user: nothing here needs to write to the image, and the ingest job never
# writes to disk at all (which is what keeps tar path traversal inapplicable).
RUN useradd --create-home --uid 10001 rainalert
USER 10001

EXPOSE 8080

# The API is the default; the job overrides it with `python -m rainalert.cli ingest`.
CMD ["sh", "-c", "uvicorn --factory rainalert.api.app:create_app --host 0.0.0.0 --port ${PORT:-8080}"]

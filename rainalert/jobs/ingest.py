"""The 5-minute ingest pipeline (DESIGN.md §6, steps 1-3).

Sampling, evaluation and rendering are M4/M5. What this does now: fetch one cycle politely, refuse
anything that looks wrong *before* it can affect state, archive the raw bytes, and record exactly
one row per nominal time.

The ordering is deliberate. Every validation happens before the cycle is treated as truth, and a
cycle that fails validation is still *recorded* - with a status and a reason - because a gap in the
timeline with no explanation is the thing that wastes an afternoon later.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from rainalert.alerting.dispatcher import deliver_queued, evaluate_cycle
from rainalert.config import Settings
from rainalert.db.models import CycleStatus, RadarCycle
from rainalert.db.session import pipeline_lock
from rainalert.radar.client import BreakerOpen, BudgetExhausted, DWDClient, FetchError
from rainalert.radar.decoder import RVFormatError, RVFrame, read_frames
from rainalert.radar.overlay import build_projection, render_frame
from rainalert.storage import ArchiveStore, OverlayStore

logger = logging.getLogger(__name__)


class CycleRejected(Exception):
    """The cycle must not be treated as truth. Carries the reason for the operator."""


@dataclass
class IngestOutcome:
    status: str
    nominal_time: datetime | None = None
    frame_count: int = 0
    bytes: int = 0
    reason: str | None = None


def validate_cycle(
    frames: list[RVFrame],
    settings: Settings,
    now: datetime,
    *,
    max_age_hours: float | None = None,
) -> None:
    """Refuse a cycle that cannot be what it claims to be.

    Raises :class:`CycleRejected`. Note what this is *not* protecting against: DWD being wrong about
    the weather. It protects against bytes that are not a plausible national composite at all -
    whether through corruption or through someone who would like a particular person not to be
    warned this afternoon.
    """
    if not frames:
        raise CycleRejected("archive contained no frames")

    stamps = {f.nominal_time for f in frames}
    if len(stamps) != 1:
        raise CycleRejected(f"archive mixes {len(stamps)} nominal times")
    nominal = stamps.pop()

    # The nominal time drives the staleness SLI (§15), and it comes from the other side. A cycle
    # stamped in the future would read as "the freshest data we ever had" and silence the alert
    # until real time caught up.
    ahead = (nominal - now).total_seconds() / 60.0
    if ahead > settings.cycle_max_future_minutes:
        raise CycleRejected(f"nominal time is {ahead:.0f} min in the future")
    # Backfill passes its own window here. Live ingest must refuse an old cycle - serving it as
    # current is how stale rain gets warned about - but a backfill is asking for old cycles on
    # purpose, and still refuses anything older than it asked for. The future check is not
    # relaxed by either.
    age_limit = settings.cycle_max_age_hours if max_age_hours is None else max_age_hours
    if -ahead > age_limit * 60:
        raise CycleRejected(f"nominal time is {-ahead / 60:.1f} h in the past")

    leads = sorted(f.lead_minutes for f in frames)
    if len(set(leads)) != len(leads):
        raise CycleRejected("duplicate forecast leads")

    # Plausibility of the field itself. Calibrated from real data: DWD_RV_FORMAT.md §8 measures the
    # no-data share at 46.7-48.5 %, and a national max of 11.8 mm/5min on a wet day.
    analysis = next((f for f in frames if f.lead_minutes == 0), None)
    if analysis is None:
        raise CycleRejected("no t+0 analysis frame")

    missing_share = float(analysis.missing.mean())
    if not settings.plausibility_missing_low <= missing_share <= settings.plausibility_missing_high:
        raise CycleRejected(f"no-data share {missing_share:.1%} outside the plausible band")

    peak = float(np.nanmax(analysis.values)) if not analysis.missing.all() else 0.0
    if peak > settings.plausibility_max_mm_5min:
        raise CycleRejected(f"national max {peak:.1f} mm/5min is implausible")


def render_overlays(
    frames: list[RVFrame], overlays: OverlayStore, *, observed_only: bool = False
) -> int:
    """Render the map frames for one cycle (§11.1).

    The analysis frame goes to the long-lived ``obs`` prefix because the timeline needs every past
    one; the forecast frames go to the short-lived ``fc`` prefix because only the newest cycle's
    forecast is ever shown.

    ``observed_only`` follows from that last clause and exists for backfill. A cycle from
    yesterday will never have its forecast displayed - the page only ever asks for the newest -
    and ``overlay_fc_retention_hours`` deletes it within the hour regardless. Rendering it is 24
    of every 25 frames of work, thrown away twice.
    """
    projection = build_projection()
    rendered = 0
    for frame in frames:
        if observed_only and frame.lead_minutes != 0:
            continue
        png = render_frame(frame, projection)
        if frame.lead_minutes == 0:
            overlays.put_observed(frame.nominal_time, png)
        else:
            overlays.put_forecast(frame.nominal_time, frame.lead_minutes, png)
        rendered += 1
    return rendered


def ingest_once(
    session: Session,
    client: DWDClient,
    store: ArchiveStore,
    settings: Settings,
    now: datetime | None = None,
    notifier=None,
    overlays: OverlayStore | None = None,
) -> IngestOutcome:
    """Run one cycle. Safe to call concurrently: the lock and the unique constraint both hold."""
    now = now or datetime.now(UTC)

    with pipeline_lock(session) as acquired:
        if not acquired:
            logger.info("another ingest run holds the lock; exiting")
            return IngestOutcome("skipped_locked")

        previous = session.execute(
            select(RadarCycle).order_by(RadarCycle.nominal_time.desc()).limit(1)
        ).scalar_one_or_none()

        try:
            result = client.fetch_latest(
                etag=previous.etag if previous else None,
                last_modified=previous.last_modified if previous else None,
            )
        except (BreakerOpen, BudgetExhausted) as exc:
            # Both of these mean "we have stopped talking to DWD", which means nobody gets warned.
            # That is an operator page, not a log line (§4.3 rule 7).
            logger.error("ingestion halted: %s", exc)
            return IngestOutcome("halted", reason=str(exc))
        except FetchError as exc:
            logger.warning("fetch failed: %s", exc)
            return IngestOutcome("fetch_failed", reason=str(exc))

        if result.not_modified or result.body is None:
            logger.info("no new cycle (304)")
            return IngestOutcome("not_modified")

        blob = result.body
        try:
            frames = read_frames(blob)
            validate_cycle(frames, settings, now)
        except (RVFormatError, CycleRejected) as exc:
            logger.error("cycle rejected: %s", exc)
            _record_rejected(session, settings, blob, result, now, str(exc))
            return IngestOutcome("rejected", bytes=len(blob), reason=str(exc))

        nominal = frames[0].nominal_time
        if session.execute(
            select(RadarCycle.id).where(RadarCycle.nominal_time == nominal)
        ).scalar_one_or_none():
            logger.info("cycle %s already stored", nominal)
            return IngestOutcome("duplicate", nominal_time=nominal)

        complete = len(frames) == settings.expected_frame_count
        archive_uri = store.put(nominal, blob)

        session.add(
            RadarCycle(
                nominal_time=nominal,
                fetched_at=now,
                source_url=client.url,
                etag=result.etag,
                last_modified=result.last_modified,
                sha256=hashlib.sha256(blob).digest(),
                bytes=len(blob),
                frame_count=len(frames),
                status=CycleStatus.OK if complete else CycleStatus.PARTIAL,
                archive_uri=archive_uri,
                notes=None
                if complete
                else f"{len(frames)} frames, expected {settings.expected_frame_count}",
            )
        )
        session.commit()

        # Rendering and evaluation both run here, against the frames already in memory (§6).
        # Rendering first and separately: a rendering failure must not cost anyone a warning.
        if overlays is not None:
            try:
                render_overlays(frames, overlays)
            except Exception:
                logger.exception("overlay rendering failed for cycle %s", nominal)

        # Evaluation runs inside the same job, against the frames already in memory (§6).
        if notifier is not None:
            cycle = session.execute(
                select(RadarCycle).where(RadarCycle.nominal_time == nominal)
            ).scalar_one()
            report = evaluate_cycle(session, cycle, frames, settings, now)
            sent, expired = deliver_queued(session, settings, notifier, now)
            if sent or expired:
                logger.info("delivered %d notification(s), expired %d", sent, expired)
            if report.blast_radius_tripped:
                logger.error("blast radius tripped for cycle %s - no mail sent", nominal)

        logger.info(
            "stored cycle %s: %d frames, %d bytes, %d attempt(s)",
            nominal,
            len(frames),
            len(blob),
            result.attempts,
        )
        return IngestOutcome(
            "ok" if complete else "partial",
            nominal_time=nominal,
            frame_count=len(frames),
            bytes=len(blob),
        )


def _record_rejected(
    session: Session,
    settings: Settings,
    blob: bytes,
    result: object,
    now: datetime,
    reason: str,
) -> None:
    """Record the rejection against a synthetic nominal time so the gap has an explanation.

    A rejected cycle has no trustworthy nominal time by definition, so it is filed under the fetch
    time floored to the 5-minute grid. If that slot is already taken by a good cycle, the good one
    wins and we only log.
    """
    slot = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
    if session.execute(
        select(RadarCycle.id).where(RadarCycle.nominal_time == slot)
    ).scalar_one_or_none():
        return
    session.add(
        RadarCycle(
            nominal_time=slot,
            fetched_at=now,
            source_url="",
            etag=None,
            last_modified=None,
            sha256=hashlib.sha256(blob).digest(),
            bytes=len(blob),
            frame_count=0,
            status=CycleStatus.REJECTED,
            archive_uri=None,
            notes=reason[:2000],
        )
    )
    session.commit()


def prune_archives(store: ArchiveStore, settings: Settings, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    return store.prune(now - timedelta(hours=settings.raw_retention_hours))


def prune_overlays(overlays: OverlayStore, settings: Settings, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    return overlays.prune(
        now - timedelta(hours=settings.overlay_obs_retention_hours),
        now - timedelta(hours=settings.overlay_fc_retention_hours),
    )

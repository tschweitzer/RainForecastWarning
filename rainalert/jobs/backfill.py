"""Fill the map timeline from archives we already hold (DESIGN.md §11.1).

A fresh deployment has no history, so the slider starts empty and fills at one frame per five
minutes. This rebuilds the observed frames from the raw archives in local storage, which touches
DWD not at all - always prefer it over re-fetching.

Fetching genuinely absent cycles *from* DWD is the other half, ``fetch_missing`` below. It is the
one place in this service that deliberately makes many requests in a row, so it is also the one
place that has to be slowest: one at a time, in order, with a jittered pause between each, inside
the same byte budget and behind the same circuit breaker as everything else.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from rainalert.config import Settings
from rainalert.db.models import CycleStatus, RadarCycle
from rainalert.jobs.ingest import CycleRejected, render_overlays, validate_cycle
from rainalert.radar.client import (
    ArchiveNotFound,
    BreakerOpen,
    BudgetExhausted,
    DWDClient,
    FetchError,
    archive_name,
)
from rainalert.radar.decoder import RVFormatError, read_frames
from rainalert.radar.overlay import build_projection, render_frame
from rainalert.storage import ArchiveStore, OverlayStore

logger = logging.getLogger(__name__)

CYCLE_MINUTES = 5

#: The pause between two backfill downloads, in seconds. Jittered rather than fixed so that two
#: instances starting together do not walk the archive in lockstep, and so the pattern does not
#: look like a machine gun with a metronome.
JITTER_MIN_SECONDS = 0.3
JITTER_MAX_SECONDS = 3.0


@dataclass
class BackfillReport:
    archives: int = 0
    rendered: int = 0
    failed: int = 0


def rerender_observed(
    archive_dir: str | Path, overlays: OverlayStore, limit: int | None = None
) -> BackfillReport:
    """Re-render the t+0 frame of every archived cycle.

    Only the analysis frame: past forecasts are not shown anywhere, so rendering them would be
    work whose output nothing reads.
    """
    report = BackfillReport()
    projection = build_projection()
    paths = sorted(Path(archive_dir).glob("DE1200_RV*.tar.bz2"), reverse=True)
    if limit:
        paths = paths[:limit]

    for path in paths:
        report.archives += 1
        try:
            frames = read_frames(path)
        except (RVFormatError, OSError):  # RVFormatError now covers tar-level damage too
            # One unreadable archive must not stop the rest; the timeline simply keeps that gap.
            report.failed += 1
            logger.warning("could not read %s", path.name, exc_info=True)
            continue
        for frame in frames:
            if frame.lead_minutes != 0:
                continue
            overlays.put_observed(frame.nominal_time, render_frame(frame, projection))
            report.rendered += 1
    logger.info(
        "re-rendered %d observed frame(s) from %d archive(s), %d unreadable",
        report.rendered,
        report.archives,
        report.failed,
    )
    return report


@dataclass
class FetchReport:
    """What a backfill run did. Every cycle asked for lands in exactly one of these."""

    wanted: int = 0
    already_held: int = 0
    fetched: int = 0
    not_retained: int = 0
    rejected: int = 0
    bytes: int = 0
    halted: str | None = None
    missing: list[datetime] = field(default_factory=list)


def missing_cycles(session: Session, hours: float, now: datetime | None = None) -> list[datetime]:
    """Every 5-minute slot in the window that has no radar_cycles row, oldest first.

    Oldest first so an interrupted run leaves a contiguous recent stretch rather than a comb.
    """
    now = now or datetime.now(UTC)
    anchor = now.replace(second=0, microsecond=0)
    anchor -= timedelta(minutes=anchor.minute % CYCLE_MINUTES)
    # Count back in whole cycles rather than subtracting the duration. `anchor - 0.1 h` lands on
    # 13:49, and every slot walked from there is off the 5-minute grid - so every request would
    # ask for a file name DWD has never published and collect a 404.
    steps = int(hours * 60 // CYCLE_MINUTES)
    start = anchor - timedelta(minutes=steps * CYCLE_MINUTES)

    held = {
        row
        for row in session.execute(
            select(RadarCycle.nominal_time).where(RadarCycle.nominal_time >= start)
        )
        .scalars()
        .all()
    }
    held = {t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC) for t in held}

    wanted: list[datetime] = []
    slot = start
    while slot <= anchor:
        if slot not in held:
            wanted.append(slot)
        slot += timedelta(minutes=CYCLE_MINUTES)
    return wanted


def fetch_missing(
    session: Session,
    client: DWDClient,
    store: ArchiveStore,
    settings: Settings,
    *,
    hours: float = 12.0,
    overlays: OverlayStore | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] | None = None,
    limit: int | None = None,
) -> FetchReport:
    """Download the cycles the timeline is missing, one at a time, slowly.

    **No alerting.** Backfilled cycles are history: evaluating them would warn people about rain
    that finished hours ago, once per cycle. Overlays are rendered because that is the whole
    point; nothing else about a cycle is acted on.

    Stops at the first sign that DWD would rather we did not continue - an exhausted byte budget
    or an open circuit breaker - and says so in the report rather than pressing on.
    """
    now = now or datetime.now(UTC)
    jitter = jitter or (lambda: random.uniform(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS))

    wanted = missing_cycles(session, hours, now)
    report = FetchReport(wanted=len(wanted))
    if limit is not None:
        wanted = wanted[:limit]

    for index, nominal in enumerate(wanted):
        # Never before the first request, always between two. A pause after the last one would
        # only make the command feel slower than it is.
        if index:
            sleep(jitter())

        name = archive_name(nominal)
        started = time.monotonic()
        try:
            result = client.fetch_named(name)
        except ArchiveNotFound:
            # Past DWD's ~48 h window, or a cycle they never published. Neither is our problem.
            logger.info("%s is not on the server", name)
            report.not_retained += 1
            report.missing.append(nominal)
            continue
        except (BreakerOpen, BudgetExhausted) as exc:
            logger.error("backfill halted: %s", exc)
            report.halted = str(exc)
            break
        except FetchError as exc:
            logger.warning("%s failed: %s", name, exc)
            report.rejected += 1
            report.missing.append(nominal)
            continue

        blob = result.body
        if blob is None:
            report.rejected += 1
            report.missing.append(nominal)
            continue

        try:
            frames = read_frames(blob)
            # The age limit is widened to the window asked for, and only that. Everything else -
            # the future check, the mixed-stamp check, the plausibility band - still applies.
            validate_cycle(frames, settings, now, max_age_hours=hours + 1)
        except (RVFormatError, CycleRejected) as exc:
            logger.error("%s rejected: %s", name, exc)
            report.rejected += 1
            report.missing.append(nominal)
            continue

        stamped = frames[0].nominal_time
        if stamped != nominal:
            # The file name said one thing and the header another. Ingest cannot make this check
            # - it asks for _LATEST and has no expectation - but here we know what we asked for.
            logger.error("%s carries nominal time %s; refusing it", name, stamped)
            report.rejected += 1
            report.missing.append(nominal)
            continue

        if session.execute(
            select(RadarCycle.id).where(RadarCycle.nominal_time == stamped)
        ).scalar_one_or_none():
            # A live ingest landed it while this run was walking the window.
            report.already_held += 1
            continue

        complete = len(frames) == settings.expected_frame_count
        session.add(
            RadarCycle(
                nominal_time=stamped,
                fetched_at=now,
                source_url=f"{client.base_url}/{name}",
                etag=result.etag,
                last_modified=result.last_modified,
                sha256=hashlib.sha256(blob).digest(),
                bytes=len(blob),
                frame_count=len(frames),
                status=CycleStatus.OK if complete else CycleStatus.PARTIAL,
                archive_uri=store.put(stamped, blob),
                notes=None
                if complete
                else f"{len(frames)} frames, expected {settings.expected_frame_count}",
            )
        )
        session.commit()
        report.fetched += 1
        report.bytes += len(blob)

        # Per cycle, with the time it took. A backfill runs for the better part of an hour, and
        # without this it is a silent process you cannot tell from a hung one - and "it feels
        # like it is getting slower" has no answer but a shrug. A request that waited on a retry
        # shows up here as seconds instead of tenths.
        elapsed = time.monotonic() - started
        logger.info(
            "%s (%d/%d) %.1f s%s",
            name,
            index + 1,
            len(wanted),
            elapsed,
            f", {result.attempts} attempts" if result.attempts > 1 else "",
        )

        if overlays is not None:
            try:
                render_overlays(frames, overlays)
            except Exception:
                logger.exception("overlay rendering failed for backfilled cycle %s", stamped)

    return report

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
import os
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
from rainalert.radar.decoder import (
    RVArchiveRejected,
    RVFormatError,
    read_analysis_frame,
    read_frames,
)
from rainalert.radar.overlay import build_projection, render_frame
from rainalert.storage import ArchiveStore, OverlayStore

logger = logging.getLogger(__name__)

CYCLE_MINUTES = 5


def _resident_megabytes() -> int:
    """This process's resident set, or 0 where /proc is not available.

    Reported per cycle because one decoded cycle is 165 MB of arrays - 25 frames of 1200x1100
    float32 plus their masks - and the machines this runs on are small. Local work collapsing
    from 2 s to 13 s while the download stays at 0.2 s is what memory pressure looks like from
    the outside, and a number beats another hypothesis.
    """
    try:
        with open("/proc/self/statm") as handle:
            return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // (1 << 20)
    except (OSError, IndexError, ValueError):
        return 0


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


def _analysis_frame(path: Path):
    """The t+0 frame of one archive, the cheap way, with the thorough way as a fallback.

    ``read_analysis_frame`` stops unpacking after the first tar member, which is the whole win
    here: bz2 is a stream, so reaching member 25 means unpacking 1 through 24 on the way, and
    that unpacking is ~96% of the time. RV writes the analysis frame first, so one member is
    all that has to come out.

    If an archive ever does not start with t+0 it raises rather than guessing, and this falls
    back to reading the whole thing - slow, but correct, and it says so in the log.
    """
    try:
        return read_analysis_frame(path)
    except RVArchiveRejected as exc:
        logger.info("%s: %s - falling back to a full read", path.name, exc)
        frames = read_frames(path, analysis_only=True)
        for frame in frames:
            if frame.lead_minutes == 0:
                return frame
        raise RVFormatError(f"{path.name} holds no analysis frame") from exc


def rerender_observed(
    archive_dir: str | Path,
    overlays: OverlayStore,
    limit: int | None = None,
    pause_seconds: float = 0.0,
) -> BackfillReport:
    """Re-render the t+0 frame of every archived cycle.

    Only the analysis frame: past forecasts are not shown anywhere, so rendering them would be
    work whose output nothing reads.

    Two things here are about memory, and both were once wrong in a way that killed the process
    on a 1 GB VM rather than merely slowing it down.

    ``analysis_only=True`` is the whole point of the docstring above. Without it every one of the
    25 members becomes a 1200x1100 float32 grid and its mask - 6.6 MB each, 165 MB a cycle - and
    24 of them are then skipped by the loop below. With it, one grid is materialised, ~6.6 MB.

    And ``frames`` is released before the next archive is read. Rebinding a loop variable frees
    the previous value only *after* the right-hand side has been evaluated, so without the
    ``del`` two whole cycles are alive at the moment the second finishes decoding - which is how
    a job that needs ~230 MB comes to need ~460 MB.
    """
    report = BackfillReport()
    projection = build_projection()
    paths = sorted(Path(archive_dir).glob("DE1200_RV*.tar.bz2"), reverse=True)
    if limit:
        paths = paths[:limit]

    logger.info("re-rendering %d archive(s) from %s", len(paths), archive_dir)
    started = time.monotonic()
    for path in paths:
        report.archives += 1
        try:
            frame = _analysis_frame(path)
        except (RVFormatError, OSError):  # RVFormatError now covers tar-level damage too
            # One unreadable archive must not stop the rest; the timeline simply keeps that gap.
            report.failed += 1
            logger.warning("could not read %s", path.name, exc_info=True)
            continue
        try:
            overlays.put_observed(frame.nominal_time, render_frame(frame, projection))
            report.rendered += 1
        finally:
            del frame
        # Progress, so a run that dies says where it got to. A silent job that is killed leaves
        # nothing to distinguish "too big" from "stuck".
        if report.archives % 25 == 0:
            done = time.monotonic() - started
            rate = done / report.archives
            logger.info(
                "  %d/%d archives, %d frames, %.0fs elapsed, ~%.0fs left",
                report.archives,
                len(paths),
                report.rendered,
                done,
                rate * (len(paths) - report.archives),
            )

        # A duty cycle, not politeness to a server: this is the only CPU-bound loop in the
        # project, and on a shared-core VM running it flat out drains the burst allowance and
        # then everything on the box - sshd included - runs at the baseline rate. Pausing
        # between archives keeps the average under that baseline, so the job takes longer and
        # the machine stays usable. 0 means go as fast as the CPU allows.
        if pause_seconds:
            time.sleep(pause_seconds)
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

    try:
        _walk(wanted, session, client, store, settings, report, overlays, now, sleep, jitter, hours)
    except KeyboardInterrupt:
        # Ctrl-C is how anyone stops a run that is going badly, and it can land anywhere - in the
        # pause, mid-download, mid-decode. Catching it around the whole walk rather than at one
        # of those points is the difference between a summary and a page of traceback. Every
        # cycle already fetched is committed, so the next run continues from there.
        logger.info("interrupted after %d cycle(s)", report.fetched)
        report.halted = "interrupted"
    return report


def _walk(
    # Many parameters, one call site: this is the body of fetch_missing, split out only so that
    # a KeyboardInterrupt anywhere inside it lands in one place. A context object would hide
    # that and buy nothing.
    wanted: list[datetime],
    session: Session,
    client: DWDClient,
    store: ArchiveStore,
    settings: Settings,
    report: FetchReport,
    overlays: OverlayStore | None,
    now: datetime,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
    hours: float,
) -> None:
    for index, nominal in enumerate(wanted):
        # Never before the first request, always between two. A pause after the last one would
        # only make the command feel slower than it is.
        waited = 0.0
        if index:
            waited = jitter()
            sleep(waited)

        name = archive_name(nominal)
        started = time.monotonic()
        try:
            result = client.fetch_named(name)
            fetched_at_s = time.monotonic()
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
            decode_started = time.monotonic()
            # Headers for all 25, the grid for the one that gets rendered. validate_cycle reads
            # every header and only the analysis frame's values, and render_overlays is called
            # with observed_only, so nothing here looks at a grid that is not built.
            frames = read_frames(blob, analysis_only=True)
            decoded_at_s = time.monotonic()
            # The age limit is widened to the window asked for, and only that. Everything else -
            # the future check, the mixed-stamp check, the plausibility band - still applies.
            validate_cycle(frames, settings, now, max_age_hours=hours + 1)
            validated_at_s = time.monotonic()
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
        stored_at_s = time.monotonic()
        report.fetched += 1
        report.bytes += len(blob)

        if overlays is not None:
            try:
                # Analysis frame only: see render_overlays. This is the difference between one
                # render per cycle and twenty-five, and on a small VM it is most of the runtime.
                render_overlays(frames, overlays, observed_only=True)
            except Exception:
                logger.exception("overlay rendering failed for backfilled cycle %s", stamped)

        # Logged last, and deliberately: wait + fetch + work is the whole cycle, so the three
        # numbers account for the time between one line and the next. An earlier version emitted
        # this before rendering, which left the most expensive local step outside the number
        # meant to measure local work - the line looked precise and quietly under-reported.
        #
        # What each one means when it grows. `wait` is ours, and only ever the jitter. `fetch` is
        # the far end or the network, and the size is there because an RV archive runs 104 KB to
        # 1.35 MB with the weather (DWD_RV_FORMAT.md §1) - so four times the seconds may be four
        # times the bytes, and only MB/s tells those apart. A `fetch` that stays flat while the
        # size grows is a fixed per-request cost, which is DNS, connection setup or the server
        # thinking, not bandwidth. `work` is this machine: decode, validate, store, render.
        now_s = time.monotonic()
        fetch_s = fetched_at_s - started
        megabytes = len(blob) / 1e6
        logger.info(
            "%s (%d/%d) wait %.1fs fetch %.1fs work %.1fs"
            " [decode %.1f validate %.1f store %.1f render %.1f] %.2fMB %.2fMB/s rss %dMB%s",
            name,
            index + 1,
            len(wanted),
            waited,
            fetch_s,
            now_s - fetched_at_s,
            decoded_at_s - decode_started,
            validated_at_s - decoded_at_s,
            stored_at_s - validated_at_s,
            now_s - stored_at_s,
            megabytes,
            megabytes / fetch_s if fetch_s > 0 else 0.0,
            _resident_megabytes(),
            f" [{result.attempts} attempts]" if result.attempts > 1 else "",
        )

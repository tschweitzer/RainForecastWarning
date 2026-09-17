"""Command line entry point.

``probe`` is the M1 acceptance tool: point it at an RV archive and a coordinate and it prints what
the service would see there, so the numbers can be checked against a public radar map during real
rain before anyone trusts an alert.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from rainalert.radar.decoder import read_frames
from rainalert.radar.grid import OutsideGrid, cell_center, cell_of, radius_mask

DEFAULT_RADIUS_M = 2000
DEFAULT_THRESHOLD = 0.1  # mm per 5 min


def _probe_cycle(frames, rows, cols, tz, args) -> None:
    print(f"cycle      {frames[0].nominal_time:%Y-%m-%d %H:%M} UTC   ({len(frames)} frames)")
    print(f"radars     {len(frames[0].radar_sites)} sites reported in the header")
    print()
    print("  lead   valid (local)     mm/5min   coverage")

    now_wet = False
    first_hit = None
    analysis_missing = False
    degraded = 0
    for frame in frames:
        window = frame.values[rows, cols]
        missing = frame.missing[rows, cols]
        local = frame.valid_time.astimezone(tz)
        if missing.all():
            print(f"  {frame.lead_minutes:+4d}   {local:%H:%M}              --   no data")
            if frame.lead_minutes == 0:
                analysis_missing = True
            degraded += 1
            continue
        peak = float(np.nanmax(window))
        cover = "full" if not missing.any() else f"{100 * (1 - missing.mean()):.0f}%"
        wet = peak >= args.threshold
        print(
            f"  {frame.lead_minutes:+4d}   {local:%H:%M}         {peak:7.2f}   {cover}"
            f"{'   rain' if wet else ''}"
        )
        if frame.lead_minutes == 0:
            now_wet = wet
        elif wet and first_hit is None:
            first_hit = frame

    print()
    # The data-quality gate comes first (DESIGN.md section 9, step 0): missing data must never be
    # reported as dry. Saying "no rain" for a location the radar cannot see is the failure this
    # whole service exists to avoid.
    if analysis_missing:
        print("NO DATA at this location - cycle would be skipped, state left unchanged")
        return
    # Mirrors the alert rule (DESIGN.md D-2): warn when rain is coming and it is dry right now.
    if now_wet:
        print("already raining here - no onset warning would be sent")
    elif first_hit is not None:
        local = first_hit.valid_time.astimezone(tz)
        print(
            f"WOULD WARN: rain from {local:%H:%M} local (+{first_hit.lead_minutes} min), "
            f"threshold {args.threshold} mm/5min"
        )
    else:
        print(f"dry, and nothing above {args.threshold} mm/5min in this cycle")
    if degraded:
        print(f"  (caution: {degraded} forecast frame(s) had no data here)")


def probe(args: argparse.Namespace) -> int:
    frames = read_frames(args.archive)
    if not frames:
        print("archive contains no frames", file=sys.stderr)
        return 1

    try:
        row, col = cell_of(args.lat, args.lon)
    except OutsideGrid as exc:
        print(f"outside the DE1200 grid: {exc}", file=sys.stderr)
        return 2

    rows, cols = radius_mask(args.lat, args.lon, args.radius)
    clat, clon = cell_center(row, col)
    tz = ZoneInfo(args.timezone)

    print(f"point      {args.lat:.4f} N {args.lon:.4f} E -> row {row}, col {col}")
    print(f"cell       {clat:.4f} N {clon:.4f} E")
    print(f"radius     {args.radius:.0f} m -> {len(rows)} cells")
    print()

    by_cycle: dict = {}
    for frame in frames:
        by_cycle.setdefault(frame.nominal_time, []).append(frame)
    for index, stamp in enumerate(sorted(by_cycle)):
        if index:
            print("-" * 52)
        _probe_cycle(by_cycle[stamp], rows, cols, tz, args)
    return 0


def ingest(args: argparse.Namespace) -> int:
    """Run one ingest cycle. This is the Cloud Run job entry point (DESIGN.md §6)."""
    import logging

    from rainalert.config import get_settings
    from rainalert.db.session import create_all, make_engine, make_session_factory
    from rainalert.jobs.ingest import ingest_once, prune_archives, prune_overlays
    from rainalert.notify import build_notifier
    from rainalert.radar.client import DWDClient
    from rainalert.storage import (
        GCSArchiveStore,
        GCSOverlayStore,
        LocalArchiveStore,
        LocalOverlayStore,
    )

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )

    engine = make_engine(settings.database_url)
    if args.create_tables:
        create_all(engine)
    session_factory = make_session_factory(engine)

    if settings.archive_dir:
        store = LocalArchiveStore(settings.archive_dir)
    elif settings.gcs_bucket:
        store = GCSArchiveStore(settings.gcs_bucket)
    else:
        print("set ARCHIVE_DIR or GCS_BUCKET", file=sys.stderr)
        return 2

    client = DWDClient(
        base_url=settings.dwd_base_url,
        latest_name=settings.dwd_latest_name,
        user_agent=settings.dwd_user_agent,
        max_response_bytes=settings.dwd_max_response_bytes,
        hourly_byte_budget=settings.dwd_hourly_byte_budget,
        daily_byte_budget=settings.dwd_daily_byte_budget,
        max_attempts=settings.dwd_max_attempts,
        backoff_base_seconds=settings.dwd_backoff_base_seconds,
        timeout_seconds=settings.dwd_request_timeout_seconds,
        breaker_threshold=settings.dwd_breaker_threshold,
        breaker_cooldown_seconds=settings.dwd_breaker_cooldown_seconds,
    )

    notifier = build_notifier(settings.notifier, settings)
    if settings.overlay_dir:
        overlays = LocalOverlayStore(settings.overlay_dir)
    elif settings.overlay_bucket:
        overlays = GCSOverlayStore(settings.overlay_bucket, settings.overlay_public_base_url or "")
    else:
        overlays = None
    with client, session_factory() as session:
        outcome = ingest_once(
            session, client, store, settings, notifier=notifier, overlays=overlays
        )
        if args.prune:
            log = logging.getLogger("rainalert.jobs.ingest")
            removed = prune_archives(store, settings)
            if removed:
                log.info("pruned %d archives", removed)
            if overlays is not None:
                removed = prune_overlays(overlays, settings)
                if removed:
                    log.info("pruned %d overlay frames", removed)

    print(f"{outcome.status}: {outcome.nominal_time or ''} {outcome.reason or ''}".strip())
    # Halted ingestion means nobody gets warned: that is a non-zero exit so the scheduler notices.
    return 0 if outcome.status in {"ok", "partial", "not_modified", "duplicate"} else 1


def verify(args: argparse.Namespace) -> int:
    """Score warnings against reality. The only honest way to tune the defaults later."""
    import logging

    from rainalert.alerting.dispatcher import purge_evaluations
    from rainalert.config import get_settings
    from rainalert.db.session import make_engine, make_session_factory
    from rainalert.jobs.verify import verify_events

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    session_factory = make_session_factory(make_engine(settings.database_url))
    with session_factory() as session:
        report = verify_events(session)
        purged = purge_evaluations(session, settings)
    rate = "n/a" if report.hit_rate is None else f"{100 * report.hit_rate:.0f}%"
    print(
        f"judged {report.judged} event(s): {report.hits} hit, {report.misses} missed, "
        f"hit rate {rate}; purged {purged} evaluation row(s)"
    )
    return 0


def rerender(args: argparse.Namespace) -> int:
    """Rebuild the map timeline from stored archives. Touches DWD not at all."""
    import logging

    from rainalert.config import get_settings
    from rainalert.jobs.backfill import rerender_observed
    from rainalert.storage import LocalOverlayStore

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    if not settings.archive_dir or not settings.overlay_dir:
        print("set ARCHIVE_DIR and OVERLAY_DIR", file=sys.stderr)
        return 2
    report = rerender_observed(
        settings.archive_dir, LocalOverlayStore(settings.overlay_dir), args.limit
    )
    print(
        f"{report.rendered} frame(s) from {report.archives} archive(s), {report.failed} unreadable"
    )
    return 0


def reset_local(args: argparse.Namespace) -> int:
    """Put local state back to empty so a test can start from nothing."""
    import logging

    from rainalert.config import get_settings
    from rainalert.db.session import make_engine
    from rainalert.jobs.reset import NotLocal, reset

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    engine = make_engine(settings.database_url)

    what = "subscribers, alerts and notifications"
    if args.all:
        what += ", AND every stored radar cycle and archive"
    if not args.yes:
        print(f"This deletes {what}.")
        print(f"Database: {engine.url.render_as_string(hide_password=True)}")
        if input("Type 'reset' to continue: ").strip() != "reset":
            print("cancelled")
            return 1

    directories = (settings.overlay_dir, settings.mail_outbox_dir)
    if args.all:
        directories += (settings.archive_dir,)

    try:
        report = reset(engine, directories, keep_radar=not args.all)
    except NotLocal as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(
        f"cleared {len(report.tables_cleared)} table(s) and "
        f"{len(report.directories_cleared)} director(ies)"
        + ("; radar data kept" if report.radar_kept else "; radar data dropped")
    )
    if report.radar_kept:
        print("run with --all to drop the radar archives too (they will be re-fetched)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rainalert")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="print what one location sees in an RV archive")
    p.add_argument("archive", type=Path)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M, help="metres")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="mm per 5 min")
    p.add_argument("--timezone", default="Europe/Berlin")
    p.set_defaults(func=probe)

    i = sub.add_parser("ingest", help="fetch, validate and archive one radar cycle")
    i.add_argument("--create-tables", action="store_true", help="create the schema if absent")
    i.add_argument("--prune", action="store_true", help="also delete archives past retention")
    i.set_defaults(func=ingest)

    v = sub.add_parser("verify", help="score past warnings against what the radar then saw")
    v.set_defaults(func=verify)

    r = sub.add_parser(
        "reset-local", help="wipe local test state (refuses anything but a local database)"
    )
    r.add_argument(
        "--all",
        action="store_true",
        help="also drop stored radar cycles and archives. Without this they are kept, because "
        "re-fetching them is 144 requests to a service DWD provides for free.",
    )
    r.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    r.set_defaults(func=reset_local)

    b = sub.add_parser(
        "rerender", help="rebuild map overlays from archives already held (no DWD traffic)"
    )
    b.add_argument("--limit", type=int, default=None, help="only the newest N archives")
    b.set_defaults(func=rerender)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

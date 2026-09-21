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
DEFAULT_THRESHOLD = 0.15  # mm per 5 min - the "leichter Regen" band (DESIGN.md §11.1.1)


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
    import os

    from rainalert.config import get_settings
    from rainalert.jobs.backfill import rerender_observed
    from rainalert.storage import LocalOverlayStore

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    if not settings.archive_dir or not settings.overlay_dir:
        print("set ARCHIVE_DIR and OVERLAY_DIR", file=sys.stderr)
        return 2

    # Set here rather than with `nice` in the Makefile, so it applies however this is started -
    # from make, from cron, or by hand. It only yields to other processes on the same machine;
    # a shared-core VM's own throttle is what --pause is for.
    try:
        os.nice(10)
    except (AttributeError, OSError):  # not POSIX, or not permitted
        pass

    report = rerender_observed(
        settings.archive_dir,
        LocalOverlayStore(settings.overlay_dir),
        args.limit,
        pause_seconds=args.pause,
    )
    print(
        f"{report.rendered} frame(s) from {report.archives} archive(s), {report.failed} unreadable"
    )
    return 0


def backfill(args: argparse.Namespace) -> int:
    """Fetch the cycles the timeline is missing. The one command here that is meant to be slow."""
    import logging

    from rainalert.config import get_settings
    from rainalert.db.session import make_engine, make_session_factory
    from rainalert.jobs.backfill import (
        JITTER_MAX_SECONDS,
        JITTER_MIN_SECONDS,
        fetch_missing,
        missing_cycles,
    )
    from rainalert.radar.client import DWDClient
    from rainalert.storage import LocalArchiveStore, LocalOverlayStore

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    if not settings.archive_dir:
        print("set ARCHIVE_DIR", file=sys.stderr)
        return 2

    hours = args.hours if args.hours is not None else settings.timeline_past_hours
    session_factory = make_session_factory(make_engine(settings.database_url))
    with session_factory() as session:
        wanted = missing_cycles(session, hours)
        if args.limit:
            wanted = wanted[: args.limit]
        if not wanted:
            print(f"nothing missing in the last {hours:g} h")
            return 0

        # Say what it is about to do before it does it. This is the one command that makes a
        # burst of requests to somebody else's free service; nobody should learn its size by
        # watching the log scroll.
        low = len(wanted) * JITTER_MIN_SECONDS / 60
        high = len(wanted) * JITTER_MAX_SECONDS / 60
        print(
            # Both ends carry their date: the window is two days by default, so "between
            # 2026-09-16 19:40 and 19:40" reads as a mistake rather than as 48 hours.
            f"{len(wanted)} cycle(s) missing between {wanted[0]:%Y-%m-%d %H:%M} and "
            f"{wanted[-1]:%Y-%m-%d %H:%M} UTC\n"
            f"one request each, {JITTER_MIN_SECONDS:g}-{JITTER_MAX_SECONDS:g}s apart: "
            f"{low:.0f}-{high:.0f} minutes, roughly {len(wanted) * 0.5:.0f} MB from DWD"
        )
        if args.dry_run:
            return 0
        if not args.yes and input("continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing fetched")
            return 1

        overlays = LocalOverlayStore(settings.overlay_dir) if settings.overlay_dir else None
        with DWDClient(
            base_url=settings.dwd_base_url,
            latest_name=settings.dwd_latest_name,
            user_agent=settings.dwd_user_agent,
            max_response_bytes=settings.dwd_max_response_bytes,
            hourly_byte_budget=settings.dwd_hourly_byte_budget,
            daily_byte_budget=settings.dwd_daily_byte_budget,
            # Gentler retries than live ingest. There, a cycle missed is a cycle gone - the
            # next one is five minutes away and the moment has passed. Here, a cycle missed is
            # picked up by the next backfill run any time in the following 48 h, so five
            # attempts at a 20 s base - up to five minutes of waiting on one archive, while the
            # run looks like it has hung - buys nothing. One retry, quickly.
            max_attempts=2,
            backoff_base_seconds=5.0,
            timeout_seconds=settings.dwd_request_timeout_seconds,
            breaker_threshold=settings.dwd_breaker_threshold,
            breaker_cooldown_seconds=settings.dwd_breaker_cooldown_seconds,
        ) as client:
            report = fetch_missing(
                session,
                client,
                LocalArchiveStore(settings.archive_dir),
                settings,
                hours=hours,
                overlays=overlays,
                limit=args.limit,
            )

    print(
        f"fetched {report.fetched}, already held {report.already_held}, "
        f"not on the server {report.not_retained}, rejected {report.rejected}, "
        f"{report.bytes / 1e6:.1f} MB"
    )
    if report.halted == "interrupted":
        # Everything fetched is committed, so re-running picks up where this left off.
        print("stopped early; run it again to continue")
        return 130
    if report.halted:
        print(f"halted early: {report.halted}", file=sys.stderr)
        return 1
    return 0


def outbox(args: argparse.Namespace) -> int:
    """Print the links from the newest file-notifier mails, decoded.

    A .eml is quoted-printable, so the confirmation link in the raw file reads
    ``token=3DHFbOJa...c=`` and continues on the next line - copying what `cat` shows produces a
    token that is wrong twice over. On a desktop a mail client hides that; on a headless box
    nothing does, and the failure looks like the token is invalid rather than mistranscribed.
    """
    import email
    import email.policy
    import re

    from rainalert.config import get_settings

    settings = get_settings()
    if not settings.mail_outbox_dir:
        print("MAIL_OUTBOX_DIR is not set (NOTIFIER=file writes there)", file=sys.stderr)
        return 2

    directory = Path(settings.mail_outbox_dir)
    if not directory.is_dir():
        print(f"no outbox at {directory}", file=sys.stderr)
        return 2

    mails = sorted(directory.glob("*.eml"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mails:
        print(f"no mail in {directory}")
        return 0

    for path in mails[: args.count]:
        message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        body = message.get_content()
        print(f"{message['To']}   {message['Subject']}")
        for url in re.findall(r"https?://\S+", body):
            print(f"  {url}")
        print()
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
        "re-fetching a full window is 577 requests to a service DWD provides for free.",
    )
    r.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    r.set_defaults(func=reset_local)

    f = sub.add_parser(
        "backfill", help="fetch past cycles the timeline is missing (many requests, slowly)"
    )
    f.add_argument(
        "--hours",
        type=float,
        default=None,
        help="how far back to fill; defaults to everything DWD keeps (~48 h)",
    )
    f.add_argument("--limit", type=int, default=None, help="at most this many cycles")
    f.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    f.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    f.set_defaults(func=backfill)

    o = sub.add_parser(
        "outbox", help="print the links from the newest local mails, decoded and ready to open"
    )
    o.add_argument("-n", "--count", type=int, default=1, help="how many mails (newest first)")
    o.set_defaults(func=outbox)

    b = sub.add_parser(
        "rerender", help="rebuild map overlays from archives already held (no DWD traffic)"
    )
    b.add_argument("--limit", type=int, default=None, help="only the newest N archives")
    b.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="seconds to wait between archives; keeps a small VM responsive at the cost of "
        "a longer run (try 0.5 on a shared-core instance)",
    )
    b.set_defaults(func=rerender)
    return parser


def _explain_connection_failure(reason: str) -> None:
    """Say what to change, rather than where SQLAlchemy gave up.

    Every command here opens a database, so a misconfigured DATABASE_URL surfaces as a hundred
    lines of connection-pool traceback whose one useful line is at the bottom. Postgres has
    already said what is wrong; this turns its sentence into the edit that fixes it.
    """
    from urllib.parse import parse_qs, urlparse

    from pydantic import ValidationError

    from rainalert.config import Settings

    try:
        url = Settings().database_url
    except ValidationError:  # configuration itself is broken; that is the better clue
        return

    parsed = urlparse(url)
    socket_dir = (parse_qs(parsed.query).get("host") or [""])[0]
    user = parsed.username or ""
    where = (
        f"the unix socket in {socket_dir!r}"
        if socket_dir
        else (f"{parsed.hostname or '?'}:{parsed.port or 5432}")
    )

    def say(text: str) -> None:
        print(text, file=sys.stderr)

    say(f"\ncould not connect to the database. DATABASE_URL points at {where}.")

    if "$(" in url or "${" in url:
        # .env is read literally by python-dotenv; a shell substitution survives as text.
        say(
            f"  The username is literally {user!r}. .env is not a shell script - it is read as\n"
            "  written, so $(whoami) is never expanded. Put your actual login name in it."
        )
    elif "Peer authentication failed" in reason:
        say(
            f"  Postgres matched your OS user against a role named {user!r} and refused.\n"
            "  Either that is not your login name, or the role does not exist:\n"
            f'    sudo -u postgres createuser --createdb --login "$(whoami)"'
        )
    elif "does not exist" in reason and "database" in reason:
        say(
            "  The server is reachable; that database has not been created:\n    createdb rainalert"
        )
    elif "does not exist" in reason and "role" in reason:
        say(
            "  The server is reachable; that role does not exist:\n"
            '    sudo -u postgres createuser --createdb --login "$(whoami)"'
        )
    elif socket_dir:
        say(
            "  Nothing is listening there. Two things put it there:\n"
            "    - the server is not running    -> macOS: brew services start postgresql@16\n"
            "                                      Linux: sudo systemctl start postgresql\n"
            "    - the socket is somewhere else -> Homebrew uses /tmp, Debian and Ubuntu use\n"
            "                                      /var/run/postgresql. Ask Postgres which:\n"
            '                                      psql -d postgres -c "show unix_socket_directories"'
        )
    else:
        say("  Check the server is running and reachable there.")

    say("docs/LOCAL.md 1.1 covers all of these.")


def main(argv: list[str] | None = None) -> int:
    from sqlalchemy.exc import OperationalError

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OperationalError as exc:
        # Only the "never got a connection" case is explained away. A database that fails
        # mid-run is a real fault and keeps its traceback.
        first = str(exc.orig).splitlines()[0] if exc.orig else ""
        if "connection is bad" not in first and "connection failed" not in first:
            raise
        print(first, file=sys.stderr)
        _explain_connection_failure(first)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

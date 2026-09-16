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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

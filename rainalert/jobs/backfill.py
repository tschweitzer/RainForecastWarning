"""Fill the map timeline from archives we already hold (DESIGN.md §11.1).

A fresh deployment has no history, so the slider starts empty and fills at one frame per five
minutes. This rebuilds the observed frames from the raw archives in local storage, which touches
DWD not at all - always prefer it over re-fetching.

Fetching genuinely absent cycles *from* DWD is the other half and is deliberately not here: it has
to obey the same politeness rules as ingest (one request at a time, spaced, inside the byte
budget), and until there is a reason to do it at deploy time the archives we keep are enough.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from rainalert.radar.decoder import RVFormatError, read_frames
from rainalert.radar.overlay import build_projection, render_frame
from rainalert.storage import OverlayStore

logger = logging.getLogger(__name__)


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

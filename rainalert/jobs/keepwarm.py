"""Keep one web instance warm by requesting it from the ingest job (DESIGN.md D-53).

The web service scales to zero, so the first visitor after ~15 quiet minutes waits for a new
instance - measured at about 8 s even after the startup probe was tightened (D-51). The ingest job
already runs every five minutes, so one request from it at the end of each run keeps an instance
from ever going idle long enough to be stopped. No new schedule, no new resource: Cloud Run bills a
request-billed instance only while it handles a request, so the cost is ~100 ms of instance time
per run, inside the free tier.

Best-effort in every direction, because this is a convenience bolted onto the job that warns
people: it never raises, never changes the job's exit code, and gives up after a bounded wait. And
it is not a guarantee - Cloud Run may still recycle an idle instance - so a cold start becomes
rare, not impossible.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

#: Long enough to cover a cold start (~8 s), so the request that starts an instance is not
#: abandoned halfway; short enough that a hung service costs the job seconds, not its timeout.
TIMEOUT_SECONDS = 15.0

#: Above this the ping found no warm instance, which is the number worth seeing in the logs:
#: if it shows up often, keeping warm is not working and min instances is the next step.
COLD_SECONDS = 2.0


def keep_warm(url: str, *, transport: httpx.BaseTransport | None = None) -> float | None:
    """Request ``url`` once and return how long it took, or None if it was skipped or failed."""
    if not url:
        return None
    started = time.monotonic()
    try:
        with httpx.Client(
            transport=transport,
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"User-Agent": "RainAlert keep-warm"},
        ) as client:
            response = client.get(url)
    except Exception as exc:  # noqa: BLE001 - nothing here may fail the job
        logger.warning("keep-warm request failed: %s", type(exc).__name__)
        return None
    elapsed = time.monotonic() - started
    if response.status_code != 200:
        logger.warning("keep-warm request answered %d", response.status_code)
    elif elapsed > COLD_SECONDS:
        logger.info("keep-warm request found the web service cold: %.1f s", elapsed)
    else:
        logger.debug("keep-warm request: %.2f s", elapsed)
    return elapsed

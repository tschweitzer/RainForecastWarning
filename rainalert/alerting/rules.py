"""Threshold and lead-time evaluation (DESIGN.md §9, step 0 and the hit rule).

Pure: no clock, no database, no network. Everything here is a function of the sampled series and
the subscription's own rule values.
"""

from __future__ import annotations

from dataclasses import dataclass

from rainalert.alerting.sampler import SampleSeries


@dataclass(frozen=True)
class AlertRule:
    threshold_mm_5min: float = 0.1
    lead_time_minutes: int = 30
    #: Above this share of no-data in the radius, a frame is not evidence of anything.
    missing_limit: float = 0.30


@dataclass(frozen=True)
class Reading:
    now_wet: bool
    first_hit_lead_minutes: int | None
    #: True when t+0 itself is unusable: the whole cycle must be skipped, state left untouched.
    analysis_missing: bool
    #: Leads that were excluded because their coverage was too poor to judge.
    gated_leads: tuple[int, ...]


def evaluate(series: SampleSeries, rule: AlertRule) -> Reading:
    """Decide what this cycle says, without deciding what to do about it."""
    rate0, missing0 = series.at(0)
    analysis_missing = rate0 is None or (missing0 is not None and missing0 > rule.missing_limit)

    now_wet = (not analysis_missing) and rate0 is not None and rate0 >= rule.threshold_mm_5min

    first_hit: int | None = None
    gated: list[int] = []
    for lead in range(5, rule.lead_time_minutes + 1, 5):
        rate, missing = series.at(lead)
        # A frame we cannot see is excluded, never counted as dry. Losing coverage at long lead
        # should delay a warning, not suppress one.
        if rate is None or (missing is not None and missing > rule.missing_limit):
            gated.append(lead)
            continue
        if rate >= rule.threshold_mm_5min and first_hit is None:
            first_hit = lead
    return Reading(now_wet, first_hit, analysis_missing, tuple(gated))

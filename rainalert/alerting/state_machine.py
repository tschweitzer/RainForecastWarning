"""The per-subscription state machine (DESIGN.md §9).

Pure functions, with time passed in. That is what makes the table in §9 testable row by row rather
than by standing outside in the rain.

The shape of the thing: a fixed cooldown either spams during showers or misses the second front.
Tying suppression to the physical event - it must go dry again for a while - gives exactly one mail
per rain event, which is what "not too many notifications" means in practice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from rainalert.alerting.rules import Reading
from rainalert.db.models import AlertState


@dataclass(frozen=True)
class StateView:
    """The stored state, as the machine sees it."""

    state: AlertState
    dry_since: datetime | None = None
    no_hit_cycles: int = 0
    last_alert_at: datetime | None = None


@dataclass(frozen=True)
class Policy:
    dry_clear_minutes: int = 30
    warned_retract_cycles: int = 3
    #: "only once per N minutes". 0 disables it - the v1 default (D-9).
    min_gap_minutes: int = 0
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    timezone: str = "Europe/Berlin"


@dataclass(frozen=True)
class Transition:
    next_state: AlertState
    dry_since: datetime | None
    no_hit_cycles: int
    alert: bool
    decision: str


def in_quiet_hours(policy: Policy, when: datetime) -> bool:
    if policy.quiet_hours_start is None or policy.quiet_hours_end is None:
        return False
    local = when.astimezone(ZoneInfo(policy.timezone)).time()
    start, end = policy.quiet_hours_start, policy.quiet_hours_end
    if start <= end:
        return start <= local < end
    return local >= start or local < end  # window wraps midnight


def advance(current: StateView, reading: Reading, policy: Policy, now: datetime) -> Transition:
    """One step of §9. ``now`` is the cycle's nominal time, not wall clock."""
    # Step 0: a cycle we cannot see must change nothing at all. Not a transition to DRY, not a
    # cleared warning - nothing. This is the difference between "no rain" and "no data".
    if reading.analysis_missing:
        return Transition(
            current.state, current.dry_since, current.no_hit_cycles, False, "skipped_missing"
        )

    state = current.state
    dry_since = now if not reading.now_wet else None
    if not reading.now_wet and current.dry_since is not None:
        dry_since = current.dry_since  # keep the earlier timestamp: the timer is cumulative

    if state is AlertState.UNKNOWN:
        nxt = AlertState.RAINING if reading.now_wet else AlertState.DRY
        return Transition(nxt, dry_since, 0, False, "observed")

    if state is AlertState.DRY:
        if reading.now_wet:
            return Transition(AlertState.RAINING, None, 0, False, "rain_started_unwarned")
        if reading.first_hit_lead_minutes is not None:
            return Transition(AlertState.WARNED, dry_since, 0, True, "alert")
        return Transition(AlertState.DRY, dry_since, 0, False, "no_rain")

    if state is AlertState.WARNED:
        if reading.now_wet:
            return Transition(AlertState.RAINING, None, 0, False, "rain_arrived")
        if reading.first_hit_lead_minutes is None:
            misses = current.no_hit_cycles + 1
            if misses >= policy.warned_retract_cycles:
                return Transition(AlertState.DRY, dry_since, 0, False, "forecast_retracted")
            return Transition(AlertState.WARNED, dry_since, misses, False, "still_warned")
        return Transition(AlertState.WARNED, dry_since, 0, False, "still_warned")

    # RAINING
    if reading.now_wet:
        return Transition(AlertState.RAINING, None, 0, False, "still_raining")
    if dry_since is not None and now - dry_since >= timedelta(minutes=policy.dry_clear_minutes):
        return Transition(AlertState.DRY, dry_since, 0, False, "cleared")
    return Transition(AlertState.RAINING, dry_since, 0, False, "drying")


def suppress(
    transition: Transition, current: StateView, policy: Policy, now: datetime
) -> Transition:
    """Apply the throttles. The state still advances - only the mail is dropped.

    That matters: if suppression rolled back the transition, the same event would re-fire on the
    next cycle and the throttle would achieve nothing.
    """
    if not transition.alert:
        return transition
    too_soon = (
        policy.min_gap_minutes
        and current.last_alert_at is not None
        and now - current.last_alert_at < timedelta(minutes=policy.min_gap_minutes)
    )
    if too_soon:
        return Transition(
            transition.next_state,
            transition.dry_since,
            transition.no_hit_cycles,
            False,
            "suppressed_gap",
        )
    if in_quiet_hours(policy, now):
        # Dropped, not queued for later: a warning delivered at 06:00 about rain at 03:00 is noise.
        return Transition(
            transition.next_state,
            transition.dry_since,
            transition.no_hit_cycles,
            False,
            "suppressed_quiet",
        )
    return transition

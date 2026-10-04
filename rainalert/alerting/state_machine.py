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
    #: Hard ceiling on alerts per rolling 24 h, independent of the rule the subscriber set.
    #:
    #: The other two throttles are opt-in and the subscriber owns them; this one they cannot turn
    #: off, which is the point. `threshold_mm_5min = 0.01`, `lead_time_minutes = 120` and
    #: `radius_m = 20000` are each individually within spec, and together they mean "warn me if a
    #: 20 km disc holds the faintest echo in the next two hours" - in German autumn, close to
    #: "always". Nothing else bounds that, because such a rule widens the definition of *event*
    #: rather than defeating a throttle (SECURITY_REVIEW.md F-15).
    #:
    #: What it protects is shared: a mail provider's daily quota and a sending domain's reputation,
    #: or - on push - the standing of one VAPID key with Google, Apple and Mozilla. One account's
    #: taste in thresholds should not cost everyone else their warnings.
    #:
    #: 0 disables it.
    alert_cap_per_day: int = 0
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
        if reading.now_wet:
            # Rain is already falling here. We have no idea whether it just started or has been
            # going for an hour, so there is nothing useful to warn about - only note the state.
            return Transition(AlertState.RAINING, None, 0, False, "observed")
        if reading.first_hit_lead_minutes is not None:
            # Dry here, rain approaching: that is exactly the thing worth saying, and we know
            # enough to say it. Waiting a cycle would leave every new subscription - and every
            # subscription that has just moved - blind for five minutes for no benefit.
            return Transition(AlertState.WARNED, dry_since, 0, True, "alert")
        return Transition(AlertState.DRY, dry_since, 0, False, "observed")

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
    transition: Transition,
    current: StateView,
    policy: Policy,
    now: datetime,
    alerts_last_day: int = 0,
) -> Transition:
    """Apply the throttles. The state still advances - only the send is dropped.

    That matters: if suppression rolled back the transition, the same event would re-fire on the
    next cycle and the throttle would achieve nothing.

    `alerts_last_day` is how many alerts this subscription has already had in the rolling 24 h, for
    `policy.alert_cap_per_day`. It is passed in rather than counted here so this stays a pure
    function of its arguments - the count is a database question and the caller owns it.
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
    # Last, deliberately. The two above are the subscriber's own preferences and should be the
    # reason recorded when they apply; this one is a limit imposed on them, and it is only
    # interesting to see in `evaluations` when nothing they chose would have stopped the send
    # anyway.
    if policy.alert_cap_per_day and alerts_last_day >= policy.alert_cap_per_day:
        return Transition(
            transition.next_state,
            transition.dry_since,
            transition.no_hit_cycles,
            False,
            "suppressed_cap",
        )
    return transition

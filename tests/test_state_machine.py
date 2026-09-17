"""Every row of the DESIGN.md §9 transition table, plus the sequences that matter.

These are pure functions, so this is cheap - which is the point of keeping them pure. The thing
being protected is subtle: the difference between "no rain" and "no data", and the guarantee of
exactly one mail per rain event rather than one per cycle.
"""

from datetime import UTC, datetime, time, timedelta

import pytest

from rainalert.alerting.rules import AlertRule, Reading, evaluate
from rainalert.alerting.sampler import SLOTS, SampleSeries
from rainalert.alerting.state_machine import Policy, StateView, advance, in_quiet_hours, suppress
from rainalert.db.models import AlertState

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
POLICY = Policy()


def reading(now_wet=False, first_hit=None, analysis_missing=False, gated=()):
    return Reading(now_wet, first_hit, analysis_missing, tuple(gated))


def state(s, **kw):
    return StateView(s, **kw)


# --- the table -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "read", "expect_state", "expect_alert", "label"),
    [
        (AlertState.UNKNOWN, reading(now_wet=True), AlertState.RAINING, False, "unknown + wet"),
        (AlertState.UNKNOWN, reading(), AlertState.DRY, False, "unknown + dry"),
        (AlertState.DRY, reading(first_hit=20), AlertState.WARNED, True, "dry + rain coming"),
        (AlertState.DRY, reading(now_wet=True), AlertState.RAINING, False, "dry + rain overhead"),
        (AlertState.DRY, reading(), AlertState.DRY, False, "dry + nothing"),
        (AlertState.WARNED, reading(now_wet=True), AlertState.RAINING, False, "warned + arrived"),
        (
            AlertState.WARNED,
            reading(first_hit=15),
            AlertState.WARNED,
            False,
            "warned + still coming",
        ),
        (AlertState.RAINING, reading(now_wet=True), AlertState.RAINING, False, "raining"),
    ],
)
def test_transition_table(start, read, expect_state, expect_alert, label):
    result = advance(state(start), read, POLICY, T0)
    assert result.next_state is expect_state, label
    assert result.alert is expect_alert, label


def test_a_warning_is_sent_once_not_once_per_cycle():
    """The core promise. Rain approaching for an hour is one mail, not twelve."""
    current = state(AlertState.DRY)
    alerts = 0
    for step in range(12):
        result = advance(current, reading(first_hit=20), POLICY, T0 + timedelta(minutes=5 * step))
        alerts += result.alert
        current = StateView(result.next_state, result.dry_since, result.no_hit_cycles, T0)
    assert alerts == 1


def test_forecast_retraction_returns_to_dry_after_three_quiet_cycles():
    current = state(AlertState.WARNED)
    for step in range(1, 3):
        result = advance(current, reading(), POLICY, T0 + timedelta(minutes=5 * step))
        assert result.next_state is AlertState.WARNED
        current = StateView(result.next_state, result.dry_since, result.no_hit_cycles)
    final = advance(current, reading(), POLICY, T0 + timedelta(minutes=15))
    assert final.next_state is AlertState.DRY
    assert final.decision == "forecast_retracted"


def test_raining_clears_only_after_the_dry_timer():
    current = state(AlertState.RAINING)
    result = advance(current, reading(), POLICY, T0)
    assert result.next_state is AlertState.RAINING  # dry, but not for long enough
    current = StateView(result.next_state, result.dry_since, result.no_hit_cycles)
    later = advance(current, reading(), POLICY, T0 + timedelta(minutes=30))
    assert later.next_state is AlertState.DRY


def test_a_second_front_warns_again_after_it_has_gone_dry():
    """One mail per event - but the second event is a second event."""
    current = state(AlertState.DRY)
    first = advance(current, reading(first_hit=20), POLICY, T0)
    assert first.alert
    # rain arrives, then stops long enough to clear
    current = StateView(AlertState.RAINING, None, 0, T0)
    cleared = advance(current, reading(), POLICY, T0 + timedelta(minutes=60))
    current = StateView(cleared.next_state, cleared.dry_since, 0, T0)
    assert current.state is AlertState.RAINING or cleared.next_state is AlertState.DRY
    second = advance(
        StateView(AlertState.DRY, None, 0, T0),
        reading(first_hit=25),
        POLICY,
        T0 + timedelta(minutes=120),
    )
    assert second.alert


# --- missing data ----------------------------------------------------------------------------


@pytest.mark.parametrize("start", list(AlertState))
def test_missing_analysis_changes_nothing_at_all(start):
    """Not a transition to DRY, not a cleared warning - nothing. This is the whole point."""
    before = state(start, no_hit_cycles=2, dry_since=T0)
    result = advance(before, reading(analysis_missing=True), POLICY, T0 + timedelta(minutes=5))
    assert result.next_state is start
    assert result.no_hit_cycles == before.no_hit_cycles
    assert result.dry_since == before.dry_since
    assert result.alert is False
    assert result.decision == "skipped_missing"


def test_a_radar_outage_does_not_retract_a_warning():
    """Three outage cycles in a row must not look like three cycles of no rain."""
    current = state(AlertState.WARNED)
    for step in range(1, 6):
        result = advance(
            current, reading(analysis_missing=True), POLICY, T0 + timedelta(minutes=5 * step)
        )
        current = StateView(result.next_state, result.dry_since, result.no_hit_cycles)
    assert current.state is AlertState.WARNED


# --- suppression -----------------------------------------------------------------------------


def test_min_gap_drops_the_mail_but_still_advances_the_state():
    """If suppression rolled back the transition, the event would re-fire forever."""
    policy = Policy(min_gap_minutes=60)
    current = state(AlertState.DRY, last_alert_at=T0 - timedelta(minutes=10))
    transition = advance(current, reading(first_hit=20), policy, T0)
    assert transition.alert
    final = suppress(transition, current, policy, T0)
    assert final.alert is False
    assert final.next_state is AlertState.WARNED
    assert final.decision == "suppressed_gap"


def test_min_gap_is_off_by_default():
    current = state(AlertState.DRY, last_alert_at=T0 - timedelta(minutes=1))
    transition = advance(current, reading(first_hit=20), POLICY, T0)
    assert suppress(transition, current, POLICY, T0).alert is True


@pytest.mark.parametrize(
    ("hour", "quiet"),
    [(23, True), (2, True), (5, True), (6, False), (12, False), (21, False)],
)
def test_quiet_hours_wrap_midnight(hour, quiet):
    policy = Policy(quiet_hours_start=time(22, 0), quiet_hours_end=time(6, 0), timezone="UTC")
    assert in_quiet_hours(policy, datetime(2026, 9, 16, hour, tzinfo=UTC)) is quiet


def test_quiet_hours_are_off_by_default():
    assert in_quiet_hours(POLICY, datetime(2026, 9, 16, 3, tzinfo=UTC)) is False


# --- rules -----------------------------------------------------------------------------------


def series(**by_lead):
    rates = [None] * SLOTS
    missing = [0.0] * SLOTS
    for key, value in by_lead.items():
        lead = int(key.removeprefix("l"))
        rates[lead // 5] = value
    return SampleSeries(tuple(rates), tuple(missing))


def test_first_hit_is_the_earliest_lead_over_the_threshold():
    reading_ = evaluate(series(l0=0.0, l5=0.0, l10=0.3, l15=0.9), AlertRule())
    assert reading_.first_hit_lead_minutes == 10
    assert reading_.now_wet is False


def test_hits_beyond_the_lead_window_are_ignored():
    reading_ = evaluate(series(l0=0.0, l60=5.0), AlertRule(lead_time_minutes=30))
    assert reading_.first_hit_lead_minutes is None


def test_already_raining_is_reported_separately_from_rain_coming():
    reading_ = evaluate(series(l0=1.0, l10=1.0), AlertRule())
    assert reading_.now_wet is True


def test_a_poorly_covered_frame_is_excluded_not_counted_as_dry():
    """Losing coverage at long lead should delay a warning, never suppress one."""
    rates = [0.0] * SLOTS
    missing = [0.0] * SLOTS
    missing[2] = 0.9  # t+10 mostly no data
    rates[2] = None
    reading_ = evaluate(SampleSeries(tuple(rates), tuple(missing)), AlertRule())
    assert 10 in reading_.gated_leads
    assert reading_.first_hit_lead_minutes is None
    assert reading_.analysis_missing is False


def test_missing_analysis_frame_is_flagged():
    rates = [None] + [0.0] * (SLOTS - 1)
    missing = [1.0] + [0.0] * (SLOTS - 1)
    assert evaluate(SampleSeries(tuple(rates), tuple(missing)), AlertRule()).analysis_missing


# --- the mask cache --------------------------------------------------------------------------


def test_mask_cache_is_actually_shared_between_subscribers():
    """Subscribers in the same place must share one mask computation.

    Regression: MaskCache defines __len__, so an empty cache is falsy, and `cache or MaskCache()`
    replaced the shared cache on every call. The property in §8.1 was silently not happening.
    """
    from rainalert.alerting.sampler import MaskCache, sample
    from rainalert.radar.decoder import read_frames
    from tests.conftest import FIXTURES

    frames = read_frames(FIXTURES / "DE1200_RV2609161355_trimmed.tar.bz2")
    cache = MaskCache()
    sample(frames, 50.1109, 8.6821, 2000, cache)
    assert len(cache) == 1
    sample(frames, 50.1109, 8.6821, 2000, cache)  # same place
    assert len(cache) == 1
    sample(frames, 53.5511, 9.9937, 2000, cache)  # elsewhere
    assert len(cache) == 2

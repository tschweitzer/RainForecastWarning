"""Is a radar cycle believable? (DESIGN.md D-70)

The numbers in these tests are the ones measured on production frames: real weather moves the wet
share by well under one percentage point per cycle, a radar site coming back adds about five, and a
corrupt "rain everywhere" cycle jumps by 85 to 95.
"""

from datetime import timedelta

import numpy as np

from rainalert.alerting.dispatcher import (
    MAX_LIFETIME,
    MIN_LIFETIME,
    RAIN_NOW_LIFETIME,
    notification_lifetime,
)
from rainalert.alerting.plausibility import WET_MM_5MIN, implausible, wet_share

LIMIT = 20.0


# --- wet share -------------------------------------------------------------------------------


def test_wet_share_counts_only_cells_with_data():
    values = np.array([[0.0, 0.5], [WET_MM_5MIN, 9.0]])
    missing = np.array([[False, False], [False, True]])
    # Three cells with data, two of them wet. The missing one is not dry and not wet.
    assert wet_share(values, missing) == 100.0 * 2 / 3


def test_wet_share_ignores_non_finite_values():
    values = np.array([0.0, np.nan, 1.0, np.inf])
    missing = np.zeros(4, dtype=bool)
    assert wet_share(values, missing) == 50.0


def test_a_frame_without_data_has_no_wet_share():
    """Not 0 %: "no data" compared with the next cycle would look like everything turned wet."""
    assert wet_share(np.zeros(4), np.ones(4, dtype=bool)) is None


# --- the judgement ---------------------------------------------------------------------------


def test_ordinary_weather_is_believed():
    assert implausible(6.3, [6.2, 6.5, 6.4], previous_share=6.1, limit_pp=LIMIT) is None


def test_a_radar_site_coming_back_in_rain_is_believed():
    assert implausible(11.0, [11.0, 10.8], previous_share=6.0, limit_pp=LIMIT) is None


def test_a_corrupt_rain_everywhere_cycle_is_not():
    reason = implausible(94.0, [94.0, 93.0], previous_share=6.0, limit_pp=LIMIT)
    assert reason is not None
    assert "6.0%" in reason and "94.0%" in reason


def test_a_corrupt_forecast_frame_is_caught_without_a_previous_cycle():
    """After a gap there is nothing to compare t+0 with - but t+0 against its own forecast still
    works, and that is the frame that would have done the warning."""
    reason = implausible(6.0, [6.1, 90.0], previous_share=None, limit_pp=LIMIT)
    assert reason is not None
    assert "forecast" in reason


def test_after_a_gap_t0_alone_is_not_judged():
    """Rain can genuinely spread across an hour of missing cycles."""
    assert implausible(45.0, [45.0, 44.0], previous_share=None, limit_pp=LIMIT) is None


def test_only_rises_count():
    """A corrupt dry cycle sends nothing; a front leaving is weather."""
    assert implausible(1.0, [0.5], previous_share=60.0, limit_pp=LIMIT) is None
    assert implausible(60.0, [10.0], previous_share=60.0, limit_pp=LIMIT) is None


def test_the_limit_is_exclusive():
    assert implausible(26.0, [26.0], previous_share=6.0, limit_pp=LIMIT) is None
    assert implausible(26.1, [26.1], previous_share=6.0, limit_pp=LIMIT) is not None


def test_zero_turns_the_check_off():
    assert implausible(94.0, [94.0], previous_share=0.0, limit_pp=0) is None


def test_a_t0_without_data_is_left_to_the_missing_data_rule():
    assert implausible(None, [90.0], previous_share=6.0, limit_pp=LIMIT) is None


# --- how long a notification is worth having -------------------------------------------------


def _warning(minutes_ahead):
    from datetime import UTC, datetime

    queued = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
    start = queued + timedelta(minutes=minutes_ahead)
    return {"predicted_start_at": start.isoformat()}, queued


def test_a_warning_is_worth_having_until_the_rain_it_announces():
    assert notification_lifetime(*_warning(45)) == timedelta(minutes=45)


def test_a_warning_for_rain_about_to_start_still_gets_a_minimum():
    """Rain a few minutes late is still rain; ten minutes covers a phone stepping out of a lift."""
    assert notification_lifetime(*_warning(2)) == MIN_LIFETIME == timedelta(minutes=10)


def test_a_far_ahead_warning_is_capped():
    assert notification_lifetime(*_warning(300)) == MAX_LIFETIME == timedelta(minutes=120)


def test_es_regnet_jetzt_is_worth_a_quarter_of_an_hour():
    payload, queued = _warning(0)
    payload["kind"] = "rain_now"
    assert notification_lifetime(payload, queued) == RAIN_NOW_LIFETIME == timedelta(minutes=15)


def test_an_unreadable_payload_gets_the_minimum_rather_than_raising():
    from datetime import UTC, datetime

    assert notification_lifetime({}, datetime.now(UTC)) == MIN_LIFETIME
    assert notification_lifetime({"predicted_start_at": "soon"}, datetime.now(UTC)) == MIN_LIFETIME

"""Regression test for the frame-0-only data-quality gate.

A real two-cycle dropout of the Borkum radar on 2026-09-15. Its value is that at 16:15 the
*analysis* frame is clean while the forecast frames are already gone - so a gate that checks only
frame 0 passes the cycle and then reads empty frames as "no rain", concluding dry for a location it
has no data for. See docs/DWD_RV_FORMAT.md section 5.
"""

import numpy as np
import pytest

from rainalert.radar.decoder import read_frames
from rainalert.radar.grid import radius_mask

BORKUM = (53.58, 6.66)  # inside the dropout
HAMBURG = (53.55, 9.99)  # control, unaffected throughout
RADIUS_M = 2000


def _by_cycle(path):
    frames = read_frames(path)
    out: dict[str, list] = {}
    for f in frames:
        out.setdefault(f.nominal_time.strftime("%H%M"), []).append(f)
    return {k: sorted(v, key=lambda f: f.lead_minutes) for k, v in out.items()}


def _missing_fraction(frame, lat, lon):
    rows, cols = radius_mask(lat, lon, RADIUS_M)
    return float(frame.missing[rows, cols].mean())


@pytest.fixture(scope="module")
def cycles(outage_cycles):
    return _by_cycle(outage_cycles)


def test_fixture_covers_the_dropout_and_its_recovery(cycles):
    assert sorted(cycles) == ["1615", "1620", "1625", "1630"]
    assert all(len(v) == 3 for v in cycles.values())


def test_analysis_is_clean_while_the_forecast_is_already_gone(cycles):
    """16:15 - the case that defeats a frame-0-only gate."""
    frames = cycles["1615"]
    assert _missing_fraction(frames[0], *BORKUM) == 0.0
    for frame in frames[1:]:
        assert _missing_fraction(frame, *BORKUM) == 1.0


def test_dropout_cycles_are_missing_at_every_lead(cycles):
    for stamp in ("1620", "1625"):
        for frame in cycles[stamp]:
            assert _missing_fraction(frame, *BORKUM) == 1.0, stamp


def test_coverage_returns(cycles):
    for frame in cycles["1630"]:
        assert _missing_fraction(frame, *BORKUM) == 0.0


def test_control_location_is_unaffected_throughout(cycles):
    """A gate that blanket-suppresses everything must fail this."""
    for stamp, frames in cycles.items():
        for frame in frames:
            assert _missing_fraction(frame, *HAMBURG) == 0.0, stamp


def test_ms_site_list_is_not_a_coverage_signal(cycles):
    """deasb and deboo are absent from MS at 16:30, yet coverage is fully restored."""
    assert "deasb" in cycles["1615"][0].radar_sites
    for stamp in ("1620", "1625", "1630"):
        assert "deasb" not in cycles[stamp][0].radar_sites
    assert _missing_fraction(cycles["1630"][0], *BORKUM) == 0.0


def test_national_extent_of_the_dropout(cycles):
    """~5.8 % of the grid goes dark, centred on the North Sea."""
    m15 = cycles["1615"][0].missing
    m20 = cycles["1620"][0].missing
    extra = m20 & ~m15
    assert int(extra.sum()) == 76514
    assert np.isclose(extra.mean(), 0.058, atol=0.001)

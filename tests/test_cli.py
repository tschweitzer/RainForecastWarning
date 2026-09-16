"""CLI behaviour, focused on what it must never say."""

from rainalert.cli import main


def test_probe_reports_a_cycle(wet_cycle, capsys):
    assert main(["probe", str(wet_cycle), "--lat", "48.1351", "--lon", "11.5820"]) == 0
    out = capsys.readouterr().out
    assert "2026-09-16 13:55 UTC" in out
    assert "row 263, col 668" in out


def test_probe_never_reports_missing_data_as_dry(outage_cycles, capsys):
    """The gate, seen from the outside: the two dark cycles must not read as 'dry'."""
    assert main(["probe", str(outage_cycles), "--lat", "53.58", "--lon", "6.66"]) == 0
    blocks = capsys.readouterr().out.split("-" * 52)
    assert len(blocks) == 4
    assert "NO DATA" in blocks[1] and "NO DATA" in blocks[2]
    assert "dry" not in blocks[1] and "dry" not in blocks[2]
    # the clean cycles either side do report dry
    assert "dry" in blocks[0] and "dry" in blocks[3]


def test_probe_flags_degraded_forecast_coverage(outage_cycles, capsys):
    """16:15: analysis clean, forecast gone - dry, but not silently."""
    main(["probe", str(outage_cycles), "--lat", "53.58", "--lon", "6.66"])
    first = capsys.readouterr().out.split("-" * 52)[0]
    assert "caution" in first and "no data here" in first


def test_probe_rejects_points_outside_the_grid(wet_cycle, capsys):
    assert main(["probe", str(wet_cycle), "--lat", "41.9", "--lon", "12.5"]) == 2
    assert "outside the DE1200 grid" in capsys.readouterr().err


def test_probe_distinguishes_raining_now_from_rain_coming(wet_cycle, capsys):
    main(["probe", str(wet_cycle), "--lat", "48.1351", "--lon", "11.5820"])
    assert "already raining here" in capsys.readouterr().out

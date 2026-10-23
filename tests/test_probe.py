"""The map's rain tooltip (DESIGN.md D-77, D-78): the colour a frame draws must say the reading it
drew.

The browser reads an overlay PNG back - palette index, colour, then the value table - instead of
asking the server. So the whole chain is checked against the renderer: `value_table` against
`palette_index`, and the browser's decoder (rainprobe.js as served, driven by tests/js/probe_test.mjs)
against the PNG `render_frame` wrote and the values `sample_frame` put in it.
"""

import io
import itertools
import json
import math
import shutil
import struct
import subprocess
import tarfile
import zlib
from pathlib import Path

import numpy as np
import pytest

from rainalert.radar import odim
from rainalert.radar.decoder import FINE_NONE
from rainalert.radar.overlay import (
    BELOW_FLOOR_STEPS,
    BOUNDS,
    DRAWN_FROM_MM_5MIN,
    FINE_BASE,
    FINE_STEPS,
    INTENSITY_BANDS,
    PALETTE,
    build_projection,
    palette_index,
    render_frame,
    sample_fine,
    sample_frame,
    value_table,
)

REPO = Path(__file__).resolve().parents[1]


# --- the table ---------------------------------------------------------------------------------


def _rows():
    table = value_table()
    return table, {tuple(row[:4]): row for row in table["rows"]}


def test_every_reading_finds_its_colour_and_its_colour_contains_it():
    """Every value RV can carry, up to well past the top band, through the renderer's own colour
    function and back through the table."""
    table, by_colour = _rows()
    readings = np.round(np.arange(1, 3001) * 0.01, 2)
    for value, rgba in zip(readings, PALETTE[palette_index(readings)], strict=True):
        row = by_colour[tuple(int(c) for c in rgba)]
        low, high = row[4], row[5]
        assert low <= value + 1e-9, (value, row)
        assert high is None or value <= high + 1e-9, (value, row)
        band = max(i for i, (start, _, _) in enumerate(INTENSITY_BANDS) if value >= start - 1e-9)
        assert table["bands"][row[6]] == INTENSITY_BANDS[band][2], value


FINE_COLOURS = {tuple(int(c) for c in PALETTE[FINE_BASE + k]) for k in range(FINE_STEPS)}


def test_colours_are_unique_and_ranges_do_not_overlap():
    table, by_colour = _rows()
    assert len(by_colour) == len(table["rows"]), (
        "two rows share a colour - the browser could not tell them apart"
    )
    # The band shades among themselves; the fine readings refine one of them (D-78), below.
    ranges = sorted((row[4], row[5]) for row in table["rows"] if tuple(row[:4]) not in FINE_COLOURS)
    for (_, high), (low, _) in itertools.pairwise(ranges):
        assert high is not None and high < low
    assert ranges[-1] == (INTENSITY_BANDS[-1][0], None), "the top band is open-ended"


def test_drizzle_readings_come_back_exactly():
    """The palest band has a shade for every one of its readings (D-60), so the tooltip can
    name them exactly - the band where a guess would be most visible."""
    _, by_colour = _rows()
    for hundredths in range(1, 15):
        value = hundredths / 100
        rgba = tuple(int(c) for c in PALETTE[palette_index(np.array([value]))[0]])
        assert by_colour[rgba][4] == by_colour[rgba][5] == value


# --- the fine drizzle readings (D-78) --------------------------------------------------------


def test_every_fine_reading_has_its_own_colour_close_to_the_drizzle_one():
    """Measurable by a program, not seen by a person: one step of red and at most nine of alpha
    from the band's own colour, the heavier reading never the fainter."""
    table, by_colour = _rows()
    base = np.array(INTENSITY_BANDS[0][1], dtype=int)
    alphas = []
    for k in range(BELOW_FLOOR_STEPS, FINE_STEPS):
        rgba = tuple(int(c) for c in PALETTE[FINE_BASE + k])
        row = by_colour[rgba]
        assert row[4] == row[5] == k / 1000
        assert table["bands"][row[6]] == INTENSITY_BANDS[0][2]
        difference = np.abs(np.array(rgba) - base)
        assert difference[:3].max() <= 1 and difference[3] <= 10, rgba
        alphas.append(rgba[3])
    assert alphas == sorted(alphas)
    assert len(PALETTE) <= 256, "a palette PNG holds at most 256 colours"


def test_below_the_floor_the_colours_are_transparent_but_still_tell_apart():
    """D-80: 0.000-0.007 are not drawn, and keep colours of their own so the debug noise cut can
    show them again - with the alpha the table's `below` names, close to the drizzle colour."""
    table = value_table()
    assert table["floor"] == DRAWN_FROM_MM_5MIN == 0.008
    assert BELOW_FLOOR_STEPS == 8 and len(table["below"]) == 8
    rgb_elsewhere = {
        tuple(int(c) for c in PALETTE[i][:3])
        for i in range(len(PALETTE))
        if not FINE_BASE <= i < FINE_BASE + BELOW_FLOOR_STEPS
    }
    base = np.array(INTENSITY_BANDS[0][1], dtype=int)
    for k, row in enumerate(table["below"]):
        entry = PALETTE[FINE_BASE + k]
        assert entry[3] == 0, "under the floor nothing is drawn"
        assert tuple(row[:3]) == tuple(int(c) for c in entry[:3])
        assert tuple(row[:3]) not in rgb_elsewhere, "the colour alone must identify the reading"
        assert row[4] == row[5] == k / 1000
        assert abs(row[3] - base[3]) <= 10 and np.abs(np.array(row[:3]) - base[:3]).max() <= 8
    # And the gradient did not move: from 0.01 up every value keeps the shade it had.
    assert palette_index(np.array([0.008, 0.009, 0.01]))[...].tolist() == [1, 1, 1]
    assert palette_index(np.array([0.007, 0.0]))[...].tolist() == [0, 0]


def test_the_decoder_keeps_the_thousandths_of_exactly_the_cells_stored_as_001(hdf5_cycle_path):
    import h5py

    with tarfile.open(hdf5_cycle_path) as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith("_000-hd5"))
        blob = archive.extractfile(member).read()
    frame = odim.decode_member(blob, member.name)
    with h5py.File(io.BytesIO(blob), "r") as h5:
        raw = h5["dataset1/data1/data"][()][::-1].astype(np.int64)
    detected = (raw != 0) & (raw != 2**32 - 1)
    low = detected & (raw - 1 < 20)
    assert np.array_equal(frame.fine != FINE_NONE, low)
    assert np.array_equal(frame.fine[low], (raw - 1)[low])
    # And those are the cells the values call 0.01 - the alerting's view is untouched.
    assert np.array_equal(np.isclose(frame.values, 0.01) & detected, low)
    assert odim.decode_member(blob, member.name, header_only=True).fine is None


def test_only_pixels_drawn_at_001_change(rendered):
    """Every other pixel keeps exactly the index it had before D-78."""
    _, sampled, fine, _, _ = rendered
    coarse, refined = palette_index(sampled), palette_index(sampled, fine)
    changed = coarse != refined
    assert changed.any(), "the fixture frame has drizzle; some pixels must be refined"
    assert (coarse[changed] == 1).all()
    assert ((refined[changed] >= FINE_BASE) & (refined[changed] < FINE_BASE + FINE_STEPS)).all()


def test_a_frame_without_fine_readings_draws_as_before(rendered):
    from dataclasses import replace

    _, sampled, _, _, frame = rendered
    assert sample_fine(replace(frame, fine=None)) is None
    assert np.array_equal(palette_index(sampled, None), palette_index(sampled))


def test_nothing_under_the_floor_is_drawn(rendered):
    """Every pixel whose reading is under 0.008 is transparent in the PNG the map gets."""
    from PIL import Image

    png, sampled, fine, _, _ = rendered
    alpha = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3]
    refined = palette_index(sampled, fine) >= FINE_BASE
    under = refined & (fine < BELOW_FLOOR_STEPS)
    assert under.sum() > 1000, "the fixture frame has drizzle under the floor"
    assert (alpha[under] == 0).all()
    assert (alpha[refined & (fine >= BELOW_FLOOR_STEPS)] > 0).all()


def test_the_floor_is_one_number():
    from rainalert.config import Settings

    assert Settings(database_url="x", _env_file=None).min_threshold_mm_5min == DRAWN_FROM_MM_5MIN


def test_the_table_stays_small():
    """It rides in every timeline manifest."""
    assert len(json.dumps(value_table(), separators=(",", ":"))) < 7000


def test_the_manifest_carries_the_table(db, tmp_path):
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.config import Settings
    from rainalert.notify import ConsoleNotifier

    settings = Settings(
        database_url="postgresql+psycopg://unused",
        overlay_dir=str(tmp_path / "overlays"),
        secret_key="test-secret",
        _env_file=None,
    )
    client = TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))
    manifest = client.get("/api/v1/overlays/timeline").json()
    assert manifest["values"] == json.loads(json.dumps(value_table()))


# --- the page loads it --------------------------------------------------------------------------


def test_the_start_page_loads_the_tooltip_after_the_map(db, tmp_path):
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.config import Settings
    from rainalert.notify import ConsoleNotifier

    settings = Settings(
        database_url="postgresql+psycopg://unused",
        overlay_dir=str(tmp_path / "overlays"),
        secret_key="test-secret",
        _env_file=None,
    )
    client = TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))
    page = client.get("/").text
    assert page.index("/static/radar.js") < page.index("/static/rainprobe.js")
    assert client.get("/static/rainprobe.js").status_code == 200
    signup = client.get("/static/signup.js").text
    assert "RainProbe.attach(Engine.probeTarget(map), radar, {" in signup
    for engine in ("radar.js", "radar-gl.js"):
        assert "probeTarget" in client.get(f"/static/{engine}").text, engine


# --- the browser's decoder ------------------------------------------------------------------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png_with_every_filter(indices: np.ndarray) -> bytes:
    """A palette PNG whose rows cycle through all five filter types, split over two IDATs.

    Pillow writes filter 0 only for these frames, so without this the four other branches of the
    browser's unfilter would never run - and a future encoder could use any of them.
    """
    height, width = indices.shape
    raw = bytearray()
    previous = np.zeros(width, dtype=np.int16)
    for y in range(height):
        row = indices[y].astype(np.int16)
        left = np.concatenate([[0], row[:-1]])
        up = previous
        up_left = np.concatenate([[0], previous[:-1]])
        kind = y % 5
        if kind == 0:
            out = row
        elif kind == 1:
            out = row - left
        elif kind == 2:
            out = row - up
        elif kind == 3:
            out = row - ((left + up) >> 1)
        else:
            p = left + up - up_left
            pa, pb, pc = np.abs(p - left), np.abs(p - up), np.abs(p - up_left)
            out = row - np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, up_left))
        raw.append(kind)
        raw += (out & 255).astype(np.uint8).tobytes()
        previous = row
    data = zlib.compress(bytes(raw))
    half = len(data) // 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
        + _chunk(b"PLTE", PALETTE[:, :3].tobytes())
        + _chunk(b"tRNS", PALETTE[:, 3].tobytes())
        + _chunk(b"IDAT", data[:half])
        + _chunk(b"IDAT", data[half:])
        + _chunk(b"IEND", b"")
    )


def _latlon_of_pixel(row: int, col: int, width: int, height: int) -> tuple[float, float]:
    (south, west), (north, east) = BOUNDS

    def merc(lat):
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

    top, bottom = merc(north), merc(south)
    y = top - (row + 0.5) / height * (top - bottom)
    lat = math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)
    lon = west + (col + 0.5) / width * (east - west)
    return lat, lon


@pytest.fixture(scope="module")
def rendered(hdf5_cycle_path):
    with tarfile.open(hdf5_cycle_path) as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith("_000-hd5"))
        frame = odim.decode_member(archive.extractfile(member).read(), member.name)
    projection = build_projection()
    return (
        render_frame(frame, projection),
        sample_frame(frame, projection),
        sample_fine(frame, projection),
        projection,
        frame,
    )


@pytest.fixture(scope="module")
def hdf5_cycle_path():
    return Path(__file__).parent / "fixtures" / "composite_rv_20261018_1740_trimmed.tar"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_browser_reads_back_what_the_renderer_drew(rendered, tmp_path, db):
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.config import Settings

    png, sampled, fine, projection, _ = rendered
    width, height = projection.width, projection.height
    frame_png = tmp_path / "frame.png"
    frame_png.write_bytes(png)

    rng = np.random.default_rng(7)
    synthetic = rng.integers(0, len(PALETTE), size=(53, 97), dtype=np.uint8)
    filtered_png = tmp_path / "filters.png"
    filtered_png.write_bytes(_png_with_every_filter(synthetic))

    expected = palette_index(sampled, fine)
    drawn = PALETTE[expected][..., 3] > 0
    wet = np.argwhere(drawn)
    dry = np.argwhere(~drawn & projection.inside)
    under = np.argwhere((expected >= FINE_BASE) & (expected < FINE_BASE + BELOW_FLOOR_STEPS))
    assert len(wet) > 1000, "the fixture frame should be a wet one"
    picks = np.concatenate(
        [
            wet[rng.choice(len(wet), 300, replace=False)],
            under[rng.choice(len(under), 50, replace=False)],
            dry[rng.choice(len(dry), 50, replace=False)],
        ]
    )
    assert (expected[tuple(picks.T)] >= FINE_BASE).sum() > 20, "the picks must include drizzle"
    places = [_latlon_of_pixel(int(r), int(c), width, height) for r, c in picks]
    heaviest = wet[np.argmax(sampled[tuple(wet.T)])]

    table = value_table()
    spec = {
        "pngs": [
            {"path": str(frame_png), "out": str(tmp_path / "frame.idx")},
            {"path": str(filtered_png), "out": str(tmp_path / "filters.idx")},
        ],
        "frame_png": str(frame_png),
        "bounds": [list(BOUNDS[0]), list(BOUNDS[1])],
        "width": width,
        "height": height,
        "places": places,
        "values": table,
        "describe": [
            {"from": 0.01, "to": 0.01, "label": "Nieselregen"},
            {"from": 0.004, "to": 0.004, "label": "Nieselregen"},
            {"from": 0, "to": 0, "label": "Nieselregen"},
            {"from": 0.63, "to": 0.64, "label": "kräftiger Regen"},
            {"from": 6.0, "to": None, "label": "extremer Starkregen"},
        ],
        "wet": _latlon_of_pixel(int(heaviest[0]), int(heaviest[1]), width, height),
        "dry": places[-1],
        "wet_label": table["bands"][
            max(
                i
                for i, (start, _, _) in enumerate(INTENSITY_BANDS)
                if sampled[tuple(heaviest)] >= start - 1e-9
            )
        ],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))

    # The served script, not the file on disk: what a browser is handed is what is tested.
    client = TestClient(
        create_app(
            Settings(database_url="postgresql+psycopg://unused", _env_file=None), session_factory=db
        )
    )
    script = tmp_path / "rainprobe.js"
    script.write_text(client.get("/static/rainprobe.js").text)

    out = tmp_path / "out.json"
    result = subprocess.run(
        [
            "node",
            str(REPO / "tests" / "js" / "probe_test.mjs"),
            str(script),
            str(spec_path),
            str(out),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"probe tests failed:\n{result.stdout}\n{result.stderr}"
    report = json.loads(out.read_text())

    # Every pixel's palette index, for the real frame and for all five row filters.
    decoded = np.fromfile(tmp_path / "frame.idx", dtype=np.uint8).reshape(height, width)
    assert np.array_equal(decoded, expected)
    unfiltered = np.fromfile(tmp_path / "filters.idx", dtype=np.uint8).reshape(synthetic.shape)
    assert np.array_equal(unfiltered, synthetic)
    assert report["decoded"][0]["width"] == width and report["decoded"][0]["height"] == height

    # Places land on the pixel they are the centre of: the browser's projection is the renderer's.
    assert report["pixels"] == [int(r) * width + int(c) for r, c in picks]

    # And what the tooltip would say there contains the value the renderer drew. Under the floor
    # it says nothing - nothing is drawn - except on the debug page (D-80).
    for (r, c), entry, debug in zip(
        picks, report["readings"], report["debug_readings"], strict=True
    ):
        value = sampled[r, c]
        if FINE_BASE <= expected[r, c] < FINE_BASE + BELOW_FLOOR_STEPS:
            assert entry is None
            assert debug["below"] and debug["from"] == debug["to"] == fine[r, c] / 1000
            continue
        assert debug == entry
        if expected[r, c] == 0:
            assert entry is None
            continue
        if expected[r, c] >= FINE_BASE:
            # A drizzle pixel says DWD's own reading, to the thousandth (D-78).
            assert entry["from"] == entry["to"] == fine[r, c] / 1000, (fine[r, c], entry)
            continue
        assert entry["from"] <= value + 1e-6, (value, entry)
        assert entry["to"] is None or value <= entry["to"] + 1e-6, (value, entry)

    drizzle, fine_reading, echo, mid, top = report["described"]
    assert drizzle["lines"] == ["ca. 0,01 mm in 5 Minuten", "≈ 0,12 mm/h · Nieselregen"]
    assert fine_reading["lines"] == ["ca. 0,004 mm in 5 Minuten", "≈ 0,048 mm/h · Nieselregen"]
    assert echo["lines"] == [
        "weniger als 0,001 mm in 5 Minuten",
        "Radarecho ohne messbare Menge · Nieselregen",
    ]
    assert (
        mid["lines"][0] == "ca. 0,64 mm in 5 Minuten"
        or mid["lines"][0] == "ca. 0,63 mm in 5 Minuten"
    )
    assert top["lines"] == [
        "6 mm oder mehr in 5 Minuten",
        "72 mm/h oder mehr · extremer Starkregen",
    ]
    assert drizzle["when"].startswith("gemessen ")
    assert report["forecast"]["when"].startswith("Vorhersage für ")
    assert report["forecast"]["lines"] == ["Hier zeigt das Radarbild keinen Regen."]

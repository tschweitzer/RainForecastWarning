"""The debug noise cut under the map (DESIGN.md D-79): below the cut nothing is drawn, above it
everything is drawn exactly as before.

The cut is applied in the browser by rewriting a frame's per-colour alpha (noisecut.js), so it is
checked on frames the renderer actually wrote: noisecut.js as served cuts them in Node
(tests/js/noisecut_test.mjs), and Pillow decodes the result here - which also proves the rewritten
chunk is a valid PNG, checksum included.
"""

import io
import json
import shutil
import struct
import subprocess
import tarfile
import zlib
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from rainalert.api.app import create_app
from rainalert.config import Settings
from rainalert.notify import ConsoleNotifier
from rainalert.radar import odim
from rainalert.radar.overlay import (
    FINE_BASE,
    PALETTE,
    build_projection,
    palette_index,
    render_frame,
    sample_fine,
    sample_frame,
    value_table,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "composite_rv_20261018_1740_trimmed.tar"


@pytest.fixture()
def client(db, tmp_path):
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        overlay_dir=str(tmp_path / "overlays"),
        secret_key="test-secret",
        _env_file=None,
    )
    return TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))


def test_the_slider_starts_at_the_floor(client):
    page = client.get("/?debug").text
    assert 'data-floor="0.008"' in page


def test_the_slider_is_only_on_the_debug_page(client):
    plain = client.get("/").text
    assert 'id="noise-cut"' not in plain and "/static/noisecut.js" not in plain
    debug = client.get("/?debug").text
    assert 'id="noise-cut"' in debug
    assert debug.index("/static/rainprobe.js") < debug.index("/static/noisecut.js")
    assert client.get("/static/noisecut.js").status_code == 200


def test_the_slider_sits_directly_under_the_map(client):
    """So the map stays in view while the cut moves: nothing between the two."""
    page = client.get("/?debug=1").text
    after_map = page[page.index('<div id="map" class="radar-map"></div>') :]
    assert after_map.index('id="noise-cut"') < after_map.index('id="radar-controls"')
    between = after_map[
        len('<div id="map" class="radar-map"></div>') : after_map.index('<div class="noise-cut"')
    ]
    assert "<div" not in between and "<p" not in between, between


def test_the_vector_map_may_fetch_the_cut_frame(client):
    """MapLibre fetches even a `blob:` image, so connect-src has to allow it."""
    policy = client.get("/").headers["content-security-policy"]
    connect = next(p for p in policy.split(";") if p.strip().startswith("connect-src"))
    assert " blob:" in connect


def test_the_page_wires_it_to_the_timeline(client):
    assert (
        "RainCut.attach(document.getElementById('noise-cut'), radar)"
        in client.get("/static/signup.js").text
    )
    radar = client.get("/static/radar.js").text
    assert "filter: function (fn)" in radar and "mine === showing" in radar


def _crc_ok(png: bytes) -> bool:
    at = 8
    while at < len(png):
        (length,) = struct.unpack(">I", png[at : at + 4])
        body = png[at + 4 : at + 8 + length]
        (crc,) = struct.unpack(">I", png[at + 8 + length : at + 12 + length])
        if zlib.crc32(body) != crc:
            return False
        at += 12 + length
    return True


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_below_the_cut_nothing_is_drawn_and_above_it_everything_is_unchanged(client, tmp_path):
    with tarfile.open(FIXTURE) as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith("_000-hd5"))
        frame = odim.decode_member(archive.extractfile(member).read(), member.name)
    projection = build_projection()
    sampled = sample_frame(frame, projection)
    fine = sample_fine(frame, projection)
    from dataclasses import replace

    pngs = {
        "fine": render_frame(frame, projection),
        # A frame without thousandths (binary edition, or before D-78): one colour for 0.000-0.019.
        "plain": render_frame(replace(frame, fine=None), projection),
    }
    index = {"fine": palette_index(sampled, fine), "plain": palette_index(sampled)}
    # What each pixel really holds: the thousandth where the frame carries it.
    truth = {"fine": np.where(index["fine"] >= FINE_BASE, fine / 1000, sampled), "plain": sampled}
    for name, png in pngs.items():
        (tmp_path / f"{name}.png").write_bytes(png)
    cuts = [0, 0.001, 0.004, 0.005, 0.008, 0.011, 0.02, 0.05, 0.1]
    spec = {
        "pngs": [{"name": n, "path": str(tmp_path / f"{n}.png")} for n in pngs],
        "values": value_table(),
        "cuts": cuts,
    }
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    scripts = {}
    for name in ("rainprobe.js", "noisecut.js"):
        scripts[name] = tmp_path / name
        scripts[name].write_text(client.get(f"/static/{name}").text)
    out = tmp_path / "out"
    out.mkdir()
    result = subprocess.run(
        [
            "node",
            str(REPO / "tests" / "js" / "noisecut_test.mjs"),
            str(scripts["rainprobe.js"]),
            str(scripts["noisecut.js"]),
            str(tmp_path / "spec.json"),
            str(out),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"noise cut tests failed:\n{result.stdout}\n{result.stderr}"
    report = json.loads((out / "report.json").read_text())

    shown_alpha = {FINE_BASE + k: row[3] for k, row in enumerate(value_table()["below"])}
    for name, png in pngs.items():
        before = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
        drawn_before = before[..., 3] > 0
        has_reading = index[name] > 0  # drawn, or under the floor (D-80)
        for cut in cuts:
            cut_png = (out / f"{name}-{cut}.png").read_bytes()
            assert _crc_ok(cut_png), (name, cut)
            after = np.array(Image.open(io.BytesIO(cut_png)).convert("RGBA"))
            visible = has_reading & (truth[name] >= cut - 1e-9)
            assert (after[~visible][:, 3] == 0).all(), f"{name} @ {cut}: something below is drawn"
            kept = visible & drawn_before
            assert np.array_equal(after[kept], before[kept]), f"{name} @ {cut}: kept pixels changed"
            # Under the floor and not under the cut: drawn again, in its own colour (D-80).
            revealed = visible & ~drawn_before
            if revealed.any():
                assert name == "fine" and cut < 0.008
                expected = PALETTE[index[name][revealed]].copy()
                expected[:, 3] = [shown_alpha[i] for i in index[name][revealed]]
                assert np.array_equal(after[revealed], expected), f"{name} @ {cut}"
            if cut == 0 and name == "fine":
                assert revealed.sum() > 1000, "cut 0 must bring back what the floor removes"

    # At the floor nothing changes - what everyone sees - so nothing is re-written either.
    assert ["fine", 0.008] in report["unchanged"] and ["plain", 0.008] in report["unchanged"]
    # The plain frame cannot split its one colour for 0.000-0.019: up to 0.01 it all stays.
    plain = np.array(Image.open(io.BytesIO(pngs["plain"])).convert("RGBA"))
    for cut in (0, 0.001, 0.005):
        assert np.array_equal(np.array(Image.open(out / f"plain-{cut}.png").convert("RGBA")), plain)
    plain_011 = np.array(Image.open(out / "plain-0.011.png").convert("RGBA"))
    assert (plain_011[np.isclose(sampled, 0.01)][:, 3] == 0).all()

    assert report["cuts"][:3] == [0, 0.001, 0.002] and report["cuts"][-1] == 0.1
    assert report["labels"][0].startswith("nichts ausgeblendet")
    assert report["labels"][report["cuts"].index(0.008)].endswith("die Rauschgrenze, wie für alle")
    assert report["labels"][report["cuts"].index(0.004)].startswith("unter 0,004 mm/5 min")
    assert "(≈ 0,6 mm/h)" in report["labels"][report["cuts"].index(0.05)]

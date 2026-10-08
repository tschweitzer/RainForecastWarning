"""The vector map: MapLibre on Shortbread tiles, the default on every page with a map (DESIGN.md
D-58 trial, D-59 default).

What a browser run showed and these keep true: both map pages load MapLibre and keep Leaflet as the
fallback; without a tile server they are plain Leaflet pages; the style the page gets names this
deployment's tile server and nobody else's; and the CSP lets MapLibre fetch tiles and overlays and
start its worker. The drawing itself - labels above the radar, the picker, the fallback without
WebGL - was verified in Chromium against synthetic Shortbread tiles; it is in the commits, not here.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rainalert.api import assets
from rainalert.api.app import create_app
from rainalert.config import Settings
from rainalert.notify import ConsoleNotifier

STATIC = Path(__file__).resolve().parents[1] / "rainalert" / "api" / "static"
TILES = "https://tiles.example.org/shortbread/{z}/{x}/{y}.mvt"


def make_client(db, tmp_path, **overrides):
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        overlay_dir=str(tmp_path / "overlays"),
        secret_key="test-secret",
        vector_tile_url=overrides.pop("vector_tile_url", TILES),
        _env_file=None,
        **overrides,
    )
    return TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))


@pytest.fixture()
def client(db, tmp_path):
    return make_client(db, tmp_path)


def test_without_a_tile_server_the_pages_are_plain_leaflet(db, tmp_path):
    client = make_client(db, tmp_path, vector_tile_url="")
    for path in ("/", "/manage"):
        page = client.get(path).text
        assert "maplibre" not in page and "radar-gl.js" not in page and "importmap" not in page, (
            path
        )
    assert 'data-map-engine="leaflet"' in client.get("/").text
    assert 'var MAP_ENGINE = "leaflet";' in client.get("/manage").text
    assert client.get("/map-style/gray.json").status_code == 404


@pytest.mark.parametrize("path", ["/", "/manage"])
def test_both_map_pages_load_maplibre_by_versioned_url_and_keep_leaflet(client, path):
    page = client.get(path).text
    importmap = re.search(
        r'<script type="importmap" nonce="[^"]+">\s*(\{.*?\})\s*</script>', page, re.DOTALL
    )
    assert importmap, "MapLibre is imported by bare name, so the page must map it"
    target = json.loads(importmap.group(1))["imports"]["maplibre-gl"]
    assert target == assets.static_url("vendor/maplibre/maplibre-gl.mjs")
    assert 'type="module" nonce="' in page and assets.static_url("radar-gl.js") in page
    # The fallback: where MapLibre cannot run, signup.js uses Leaflet, so Leaflet must be here.
    assert assets.static_url("vendor/leaflet/leaflet.js") in page
    # After radar.js, whose timeline and pin the module reuses.
    assert page.index(assets.static_url("radar.js")) < page.index("radar-gl.js")


def test_each_page_asks_for_the_vector_engine(client):
    assert 'data-map-engine="vector"' in client.get("/").text
    # The start page's script runs `defer`, after the module; the settings page's runs inline, so
    # it must wait for the module before choosing - or a quick session reply builds Leaflet.
    page = client.get("/").text
    assert page.index("radar-gl.js") < page.index("signup.js")
    manage = client.get("/manage").text
    assert 'var MAP_ENGINE = "vector";' in manage
    build = manage[manage.index("function buildMap()") :]
    assert build.index("if (!parsed) {") < build.index("Engine.createMap(")
    # The event, not readyState: 'interactive' arrives before the module has run.
    assert (
        "document.addEventListener('DOMContentLoaded', function () { parsed = true; });" in manage
    )
    assert "readyState" not in build


@pytest.mark.parametrize("theme", ["gray", "gray-dark"])
def test_the_style_names_this_deployments_tiles_and_fonts_only(client, theme):
    response = client.get(f"/map-style/{theme}.json")
    assert response.status_code == 200
    style = response.json()

    vector = [s for s in style["sources"].values() if s["type"] == "vector"]
    assert len(vector) == 1
    assert vector[0]["tiles"] == [TILES]
    assert "url" not in vector[0], "a TileJSON address would be fetched from wherever it points"
    assert vector[0]["maxzoom"] == 14, "Shortbread stops at 14; beyond it MapLibre must overzoom"
    assert "OpenStreetMap" in vector[0]["attribution"]

    # Nothing from anywhere else: no glyph server, no sprite, nothing that would ask for one.
    assert "glyphs" not in style and "sprite" not in style
    text = json.dumps(style)
    assert "versatiles.org" not in text
    for layer in style["layers"]:
        assert "icon-image" not in layer.get("layout", {}), layer["id"]
        assert not {"fill-pattern", "line-pattern"} & set(layer.get("paint", {})), layer["id"]

    # Fonts by versioned URL, and every one of them actually served.
    urls = [face["url"] for faces in style["font-faces"].values() for face in faces]
    assert urls
    for url in urls:
        assert re.fullmatch(r"/static/vendor/fonts/noto-sans/[\w-]+\.woff2\?v=[0-9a-f]{12}", url)
        assert client.get(url).status_code == 200

    # radar-gl.js puts the radar just under the labels using this layer.
    assert any(layer["id"] == "slot-below-labels" for layer in style["layers"])


def test_only_the_two_themes_are_served(client):
    assert client.get("/map-style/colorful.json").status_code == 404
    assert client.get("/map-style/..%2fgray.json").status_code == 404


def test_the_csp_lets_maplibre_work(db, tmp_path):
    client = make_client(db, tmp_path, overlay_public_base_url="https://storage.googleapis.com/b")
    csp = client.get("/").headers["content-security-policy"]
    directives = {d.split()[0]: d.split()[1:] for d in csp.split("; ")}
    # Tiles and overlays come through fetch(), so connect-src, not only img-src.
    assert "https://tiles.example.org" in directives["connect-src"]
    assert "https://storage.googleapis.com" in directives["connect-src"]
    assert "blob:" in directives["img-src"]
    # The worker is the vendored module next to the library: same origin, no blob: needed.
    assert directives["worker-src"] == ["'self'"]


def test_tiles_get_the_origin_as_referer_and_nothing_else_does():
    """The pages send no Referer at all; tile servers want one. The exception is tiles only, and
    the origin only - the same one the Leaflet tiles make."""
    gl = (STATIC / "radar-gl.js").read_text(encoding="utf-8")
    assert (
        "resourceType === 'Tile' ? { url, referrerPolicy: 'strict-origin-when-cross-origin' } : { url }"
        in gl
    )


#: sha256 of each vendored file as published (static/vendor/*/README.md says where from).
VENDORED = {
    "vendor/maplibre/maplibre-gl.mjs": "389731e8581cc59d484e867b051db73ae034a76f0691d2890a2a1a7d2495bdf3",
    "vendor/maplibre/maplibre-gl-worker.mjs": "53a2dd60f76c7b9201a3cc007b755d98ce4b52403843fef5c90c41780fe62e91",
    "vendor/maplibre/maplibre-gl.css": "20fc3054e6c769ae9502ec5c7ea46dae05b544fdc4fca2108553fb8c07265bf3",
    "vendor/fonts/noto-sans/noto-sans-latin-400-normal.woff2": "09aee8065d25508f23a4c3d92cd777ac869c52d93fd868a88f025d888a7937d6",
    "vendor/fonts/noto-sans/noto-sans-latin-700-normal.woff2": "e77bfe1db912f687b0319b60de158cfada67f89c8ee4f8e2bd6020f970accbfb",
    "vendor/fonts/noto-sans/noto-sans-latin-ext-400-normal.woff2": "c8e6bf928ae88c948ebc49dbe1df01ea5505d12408904ea3302309b26487fbca",
    "vendor/fonts/noto-sans/noto-sans-latin-ext-700-normal.woff2": "c0cd431266a7ed82d6f0dfe281e38a883c28a03eec1fdac308cc2f7c0ea39c3b",
}


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_the_vendored_files_are_byte_for_byte_upstream(name):
    digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
    assert digest == VENDORED[name], f"{name} is not the published file - see its README"


def test_a_tap_on_the_pin_does_not_move_it():
    """MapLibre puts markers inside the map's container, so a tap on the pin reached the picker's
    map-click handler at the tap's coordinates - above the tip - and moved the pin ~30 km north per
    tap at country zoom. Found by tapping it three times in Chromium; Leaflet never had this."""
    from tests.helpers import js_function

    picker = js_function((STATIC / "radar-gl.js").read_text(encoding="utf-8"), "picker")
    handler = picker[picker.index("view.gl.on('click'") :]
    assert handler.index("closest('.maplibregl-marker')") < handler.index("place(event.lngLat")

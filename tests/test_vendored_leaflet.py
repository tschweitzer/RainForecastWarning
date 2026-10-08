"""Leaflet is served by this app, and the basemap defaults to one that costs nobody anything.

Two changes land together here because they are the same concern from two directions. Until
2026-09-27 a visit to the radar told two third parties who was looking: unpkg, for the library, and
whatever tile server an operator had configured. The library half is now vendored under
`static/vendor/leaflet`, and the tile half has a default that is public open data rather than
somebody's donated bandwidth.

What these tests are actually defending:

* The vendored files are upstream's, byte for byte. A vendored dependency that quietly drifts is
  worse than a CDN, because nobody can diff it against anything.
* The policy names no third-party script or style origin any more. That is the whole point of
  vendoring, and it is one careless `MAP_SCRIPT_SRC`-shaped constant away from coming back.
* The default tile template has `{y}` before `{x}`. basemap.de is WMTS. Swapping those two
  renders a *scrambled* map rather than an error, which is the kind of mistake that survives a
  code review and a test suite that only checks for a 200.
"""

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rainalert.api.app import create_app, image_origin
from rainalert.config import Settings
from rainalert.notify import ConsoleNotifier
from tests.helpers import page_source

VENDOR = Path(__file__).resolve().parents[1] / "rainalert" / "api" / "static" / "vendor" / "leaflet"

#: sha256 of the two files as published in the leaflet@1.9.4 npm tarball, which is also what
#: unpkg serves. The tarball itself was checked against the registry's own sha1 and sha512 when
#: it was vendored - see VENDOR/README.md, which records both. Bumping Leaflet means changing
#: these in the same commit, deliberately, rather than discovering later that a local edit is
#: now upstream as far as anyone can tell.
UPSTREAM_SHA256 = {
    "leaflet.js": "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a",
    "leaflet.css": "a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6",
}

MAP_PAGES = ("/", "/manage")


@pytest.fixture()
def settings():
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.invalid",
        secret_key="test-secret",
        notifier="console",
        _env_file=None,
    )


@pytest.fixture()
def client(db, settings):
    return TestClient(
        create_app(settings, session_factory=db, notifier=ConsoleNotifier()),
        base_url=settings.public_base_url,
    )


# --- the vendored files themselves ---------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(UPSTREAM_SHA256))
def test_the_vendored_leaflet_is_byte_for_byte_upstream(name):
    digest = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
    assert digest == UPSTREAM_SHA256[name], (
        f"{name} is not the file leaflet@1.9.4 published. Either it was edited - don't, change "
        f"radar.js instead - or Leaflet was bumped without updating UPSTREAM_SHA256 and "
        f"{VENDOR.name}/README.md. `make vendor-leaflet` restores it."
    )


def test_the_licence_travels_with_the_code():
    """BSD-2-Clause asks for the copyright notice to be retained. Summarising it in a comment is
    not retaining it, so the upstream file is here verbatim."""
    licence = (VENDOR / "LICENSE").read_text(encoding="utf-8")
    assert "BSD 2-Clause" in licence
    assert "Volodymyr Agafonkin" in licence


def test_every_image_the_stylesheet_asks_for_is_vendored():
    """`leaflet.css` refers to images by relative path, so vendoring the CSS without them turns
    every one into a 404 against our own origin.

    Written as "parse the stylesheet and check what it actually asks for" rather than "assert
    these three filenames exist", because the point is to notice a *new* reference after a
    version bump - which a hard-coded list would sail straight past.
    """
    css = (VENDOR / "leaflet.css").read_text(encoding="utf-8")
    referenced = {
        match
        for match in re.findall(r"url\(([^)]+)\)", css)
        # url(#default#VML) is an old-IE behaviour hook, not a file.
        if not match.strip().startswith("#")
    }
    assert referenced, "no url() references found - did the stylesheet change shape?"
    for ref in referenced:
        target = VENDOR / ref.strip("'\"")
        assert target.is_file(), f"leaflet.css references {ref}, which is not vendored"


def test_nothing_unnecessary_was_vendored():
    """The counterpart to the test above: files that nothing fetches are files that exist to be
    fetched by nothing.

    `marker-shadow.png` and `marker-icon-2x.png` are unreferenced because the map pin is an
    inline-SVG divIcon, and `leaflet.js.map` cannot resolve anything without the whole `src/`
    tree it names. Deliberate absences, recorded so a future "completeness" tidy-up has to argue
    with a test rather than just add 1.1 MB back.
    """
    present = {path.name for path in VENDOR.rglob("*") if path.is_file()}
    for unwanted in ("marker-shadow.png", "marker-icon-2x.png", "leaflet.js.map", "leaflet-src.js"):
        assert unwanted not in present, f"{unwanted} is vendored but nothing fetches it"


# --- served, and used ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/vendor/leaflet/leaflet.js", "javascript"),
        ("/static/vendor/leaflet/leaflet.css", "css"),
        ("/static/vendor/leaflet/images/marker-icon.png", "image/png"),
    ],
)
def test_the_vendored_assets_are_served(client, path, content_type):
    response = client.get(path)
    assert response.status_code == 200, path
    assert content_type in response.headers["content-type"]


@pytest.mark.parametrize("path", MAP_PAGES)
def test_every_map_page_loads_leaflet_from_this_app(client, path):
    body = client.get(path).text
    # With the content version D-57 adds, so a vendored upgrade reaches browsers that cached the
    # old one.
    assert re.search(
        r'<link rel="stylesheet" href="/static/vendor/leaflet/leaflet\.css\?v=[0-9a-f]{12}">', body
    )
    assert re.search(r'src="/static/vendor/leaflet/leaflet\.js\?v=[0-9a-f]{12}"', body)


@pytest.mark.parametrize("path", MAP_PAGES)
def test_no_page_fetches_a_script_or_stylesheet_from_anywhere_else(client, path):
    """The failure this replaces: `MAP_SCRIPT_SRC = "https://unpkg.com"`, sitting in both
    `script-src` and `style-src` because the pages genuinely needed it.

    Checks the markup rather than only the header, because a header that allows nothing and a
    page that asks for something is a broken page, and a page that asks for nothing while the
    header still allows a CDN is a policy that has outlived its reason.
    """
    body = client.get(path).text
    for tag in re.findall(r"<(?:script|link)\b[^>]*>", body):
        for attr in re.findall(r'(?:src|href)="([^"]+)"', tag):
            assert not attr.startswith(("http://", "https://", "//")), (
                f"{path} loads {attr} from another origin"
            )


def test_the_policy_names_no_third_party_script_or_style_origin(client):
    policy = client.get("/").headers["content-security-policy"]
    for directive in ("script-src", "style-src"):
        part = next(p.strip() for p in policy.split(";") if p.strip().startswith(directive))
        assert "http" not in part, f"{part} still allows a third party"
    assert "unpkg" not in policy


# --- the default basemap ------------------------------------------------------------------------


def test_the_default_basemap_is_basemap_de(settings):
    """Q-5, resolved: BKG's own basemap. CC BY 4.0, no key, no account, no quota, and - the part
    that survives this service ever carrying ads - no non-commercial clause."""
    assert "sgx.geodatenzentrum.de" in settings.map_tile_url
    assert "de_basemapde_web_raster" in settings.map_tile_url


def test_the_default_basemap_template_puts_y_before_x(settings):
    """The mistake this exists to catch.

    basemap.de is WMTS, whose tile path is `{z}/{y}/{x}`. Most OSM-derived providers are
    `{z}/{x}/{y}`. Feed Leaflet the wrong one and it renders a *scrambled* map at a plausible
    zoom - every request returns 200, every tile is a real tile, and none of them is in the right
    place. Nothing else in the suite would notice.
    """
    order = re.search(r"\{z\}/\{([xy])\}/\{([xy])\}", settings.map_tile_url)
    assert order, f"no {{z}}/{{?}}/{{?}} triple in {settings.map_tile_url!r}"
    assert order.groups() == ("y", "x"), (
        "basemap.de is WMTS and wants {z}/{y}/{x}; this template has "
        f"{{z}}/{{{order.group(1)}}}/{{{order.group(2)}}}"
    )


def test_the_default_basemap_uses_the_web_mercator_matrix_set(settings):
    """`GLOBAL_WEBMERCATOR` is the one that lines up with Leaflet's default CRS. The service also
    publishes `DE_EPSG_25832_ADV`, which is UTM32 - it returns tiles happily and they do not
    match the projection the radar overlay is drawn in."""
    assert "GLOBAL_WEBMERCATOR" in settings.map_tile_url
    assert "25832" not in settings.map_tile_url


def test_the_default_attribution_credits_bkg_and_links_the_licence(settings):
    """CC BY 4.0 wants the licence named and linked, and BKG asks that its name link to its own
    site. A credit that is only a string satisfies neither."""
    attribution = settings.map_tile_attribution
    assert "BKG" in attribution
    assert 'href="https://www.bkg.bund.de"' in attribution
    assert 'href="https://creativecommons.org/licenses/by/4.0/"' in attribution


def test_a_configured_basemap_always_has_a_credit(settings):
    """Terraform enforces this pairing too (`infra/variables.tf`), but nothing stops someone
    setting the env vars directly, and tiles on screen with no credit breaches every provider's
    terms including this one's."""
    assert settings.map_tile_url and settings.map_tile_attribution


@pytest.mark.parametrize("path", MAP_PAGES)
def test_the_policy_allows_exactly_the_configured_tile_host(client, path):
    policy = client.get(path).headers["content-security-policy"]
    img_src = next(p.strip() for p in policy.split(";") if p.strip().startswith("img-src"))
    assert "https://sgx.geodatenzentrum.de" in img_src
    # Narrow, not open: the whole reason image_origin() derives this rather than hard-coding it.
    assert "*" not in img_src


def test_the_tile_host_is_the_only_thing_the_default_adds_to_the_policy(settings):
    assert image_origin(settings.map_tile_url) == "https://sgx.geodatenzentrum.de"


def test_no_basemap_is_still_a_supported_state(db, settings):
    """The graticule-and-cities fallback was the default until this change and remains the
    opt-out. Somebody who does not want a German federal agency in their request path - or wants
    the map to work with no outbound network at all - clears both settings and gets a map that
    still reads a rain field.
    """
    bare = settings.model_copy(update={"map_tile_url": "", "map_tile_attribution": ""})
    client = TestClient(
        create_app(bare, session_factory=db, notifier=ConsoleNotifier()),
        base_url=bare.public_base_url,
    )
    body = page_source(client)
    assert "graticule" in body
    assert "sgx.geodatenzentrum.de" not in body
    policy = client.get("/").headers["content-security-policy"]
    img_src = next(p.strip() for p in policy.split(";") if p.strip().startswith("img-src"))
    assert "geodatenzentrum" not in img_src

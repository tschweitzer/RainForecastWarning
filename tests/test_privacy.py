"""The privacy page against what the service actually does (DESIGN.md D-72).

A privacy statement is only worth something while it is true, and the way it stops being true is
quietly: a new browser store, a changed retention, a new recipient - each added in code, none of
them in the text. These tests tie the page to the code where they can.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rainalert.api.app import create_app
from rainalert.config import Settings
from rainalert.notify import ConsoleNotifier

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "rainalert" / "api" / "static"


def privacy(db, tmp_path, **overrides) -> str:
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        overlay_dir=str(tmp_path / "overlays"),
        secret_key="test-secret",
        _env_file=None,
        **overrides,
    )
    client = TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))
    return client.get("/privacy").text


# --- Art. 13: who, why, on what basis, which rights ----------------------------------------------


def test_the_controller_is_named_when_configured(db, tmp_path):
    page = privacy(
        db,
        tmp_path,
        privacy_controller="Erika Muster, Musterweg 1, 12345 Musterstadt",
        contact_email="rain@example.org",
    )
    assert "<h2>Verantwortlich</h2>" in page
    assert "Erika Muster, Musterweg 1, 12345 Musterstadt" in page
    assert 'href="mailto:rain@example.org">rain@example.org</a>' in page


def test_without_a_configured_controller_the_page_still_says_who(db, tmp_path):
    assert "Der Betreiber\ndieser Website" in privacy(db, tmp_path)


def test_every_legal_basis_and_right_is_named(db, tmp_path):
    page = privacy(db, tmp_path)
    for basis in ("lit. a", "lit. c", "lit. f", "§ 25 Abs. 2 Nr. 2"):
        assert basis in page, basis
    for article in ("Art. 15", "Art. 16", "Art. 17", "Art. 18", "Art. 20", "Art. 21", "Art. 77"):
        assert article in page, article
    assert "widerrufen" in page.lower()


# --- the numbers come from the settings they describe ----------------------------------------


def test_retention_periods_follow_the_settings(db, tmp_path):
    page = privacy(
        db,
        tmp_path,
        evaluation_retention_hours=12,
        rate_limit_retention_days=3,
        unconfirmed_purge_hours=6,
        manage_session_ttl_minutes=20,
        manage_session_max_minutes=90,
        confirm_token_ttl_hours=5,
    )
    assert "Das Protokoll der Auswertungen: 12 Stunden" in page
    assert "Missbrauch: 3 Tage" in page
    assert "nach 6 Stunden" in page
    assert "20 Minuten angemeldet" in page
    assert "höchstens\n      90 Minuten" in page
    assert "höchstens 5 Stunden" in page


def test_the_abuse_counters_are_disclosed_with_the_ip_address(db, tmp_path):
    """They hold a salted hash of the client IP (D-73) and outlive the account (F-5)."""
    page = privacy(db, tmp_path)
    assert "Zum Schutz vor Missbrauch" in page
    assert "gesalzenen Hash deiner IP-Adresse wie oben" in page
    assert "IP-Adresse selbst speichern wir dafür nicht" in page
    assert "auch nach dem\n      Abmelden" in page


# --- recipients --------------------------------------------------------------------------------


def test_the_mail_server_is_named_only_when_email_is_on(db, tmp_path):
    on = privacy(db, tmp_path, email_channel_enabled=True, smtp_host="smtp.example.org")
    assert "<code>smtp.example.org</code>" in on
    off = privacy(db, tmp_path, email_channel_enabled=False, smtp_host="smtp.example.org")
    assert "smtp.example.org" not in off


def test_every_push_service_the_allowlist_accepts_is_named(db, tmp_path):
    """A push host we will send to is a recipient of the push address."""
    page = privacy(db, tmp_path)
    for company in ("Google", "Mozilla", "Apple", "Microsoft"):
        assert company in page, company


# --- what the browser stores -------------------------------------------------------------------

#: Every place the front end stores something in the browser, as (file, API) pairs. The privacy
#: page's "Was dein Browser speichert" is written from this list. A new pair fails the test below
#: on purpose: add the store to the page, then here.
BROWSER_STORES = {
    ("devicekey.js", "indexedDB"),  # the private half of the device key (D-64)
    ("pendingconfirm.js", "indexedDB"),  # the confirmation link the worker kept (D-65)
    ("sw.js", "indexedDB"),  # the same store, written by the worker
    ("signup.js", "localStorage"),  # the chosen time range
    ("signup.js", "sessionStorage"),  # "Abgemeldet" after a deletion, read once
    ("settings.js", "sessionStorage"),  # the same flag, written
}
STORE_API = re.compile(
    r"\b(localStorage|sessionStorage|indexedDB|document\.cookie|caches|cookieStore)\b"
)


def test_no_browser_store_goes_unmentioned_on_the_privacy_page():
    found = set()
    for script in STATIC.glob("*.js"):
        for line in script.read_text(encoding="utf-8").splitlines():
            code = line.split("//")[0]
            for api in STORE_API.findall(code):
                found.add((script.name, api))
    # Comments in block form mention the APIs too; only real call sites count.
    found = {pair for pair in found if pair in BROWSER_STORES or _called(pair)}
    assert found == BROWSER_STORES, (
        "browser storage changed - update privacy.html 'Was dein Browser speichert', then "
        "BROWSER_STORES"
    )


def _called(pair) -> bool:
    name, api = pair
    text = (STATIC / name).read_text(encoding="utf-8")
    return re.search(rf"\b{re.escape(api)}\s*[.(=]", text) is not None


def test_the_only_cookie_is_the_one_the_page_names(db, tmp_path):
    from rainalert.api.app import MANAGE_COOKIE

    calls = [
        path
        for path in (ROOT / "rainalert").rglob("*.py")
        if "set_cookie(" in path.read_text(encoding="utf-8")
    ]
    assert [p.name for p in calls] == ["app.py"], "a second place sets cookies"
    page = privacy(db, tmp_path)
    assert f"<code>{MANAGE_COOKIE}</code>" in page
    assert "kein\nCookie-Banner" in page or "kein Cookie-Banner" in page


@pytest.mark.parametrize("phrase", ["Service Worker", "Zeitraum", "privaten Teil des Schlüssels"])
def test_the_browser_section_lists_each_store(db, tmp_path, phrase):
    assert phrase in privacy(db, tmp_path)

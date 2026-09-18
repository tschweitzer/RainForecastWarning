"""The stores, driven the way the documented configuration drives them.

Every other test builds these with pytest's ``tmp_path``, which is absolute. ``ARCHIVE_DIR`` in
docs/LOCAL.md is ``./var/raw``, which is not - and that difference was enough to hide a crash on
the very first real cycle.
"""

from datetime import UTC, datetime

from rainalert.storage import LocalArchiveStore, LocalOverlayStore

NOMINAL = datetime(2026, 9, 16, 13, 55, tzinfo=UTC)


def test_a_relative_archive_dir_yields_a_usable_uri(tmp_path, monkeypatch):
    """`Path.as_uri()` refuses a relative path, so the root has to be resolved on the way in."""
    monkeypatch.chdir(tmp_path)
    store = LocalArchiveStore("./var/raw")

    uri = store.put(NOMINAL, b"payload")

    assert uri.startswith("file://")
    assert uri.endswith("/DE1200_RV2609161355.tar.bz2")
    assert (tmp_path / "var/raw/DE1200_RV2609161355.tar.bz2").read_bytes() == b"payload"


def test_archives_stay_put_when_the_process_changes_directory(tmp_path, monkeypatch):
    """A relative root that is re-interpreted later writes cycles into the wrong place."""
    monkeypatch.chdir(tmp_path)
    store = LocalArchiveStore("./var/raw")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    store.put(NOMINAL, b"payload")

    assert (tmp_path / "var/raw/DE1200_RV2609161355.tar.bz2").exists()
    assert not (elsewhere / "var/raw").exists()


def test_overlays_survive_a_relative_root_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = LocalOverlayStore("./var/overlays")

    url = store.put_observed(NOMINAL, b"png")

    # The URL is served by the app and stays relative; only the path on disk is resolved.
    assert url == "/overlays/obs/20260916T1355.png"
    assert (tmp_path / "var/overlays/obs/20260916T1355.png").read_bytes() == b"png"

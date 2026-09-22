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


def _env(monkeypatch, tmp_path, dsn: str) -> None:
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("SECRET_KEY", "test")
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("NOTIFIER", "file")
    monkeypatch.setenv("MAIL_OUTBOX_DIR", str(tmp_path / "outbox"))


def test_unreachable_database_is_explained_not_dumped(monkeypatch, tmp_path, capsys):
    """A mistyped socket path is a one-line mistake and must not read as a crash.

    No stubbing: this is the real connect, to a socket directory that is simply empty. It reaches
    the database before it would reach DWD, so nothing is fetched.
    """
    _env(monkeypatch, tmp_path, f"postgresql+psycopg://me@/rainalert?host={tmp_path}")
    assert main(["ingest"]) == 1
    err = capsys.readouterr().err
    assert "could not connect to the database" in err
    assert str(tmp_path) in err  # the socket directory it actually tried
    assert "/var/run/postgresql" in err  # the fix it is almost always asking for
    assert "Traceback" not in err


def test_a_shell_substitution_in_env_is_named_as_the_cause(monkeypatch, tmp_path, capsys):
    """.env is read literally, so `$(whoami)` reaches Postgres as a username.

    Postgres then says "Peer authentication failed for user ..." - true, and no help at all
    unless you notice what the name is.
    """
    _env(monkeypatch, tmp_path, f"postgresql+psycopg://$(whoami)@/rainalert?host={tmp_path}")
    assert main(["ingest"]) == 1
    err = capsys.readouterr().err
    assert "not a shell script" in err
    assert "$(whoami)" in err


def test_env_files_really_are_read_literally(tmp_path, monkeypatch):
    """The premise of the message above. If dotenv ever learned to expand, it should fail here."""
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://$(whoami)@/db\nSECRET_KEY=x\n"
    )
    monkeypatch.chdir(tmp_path)
    for var in ("DATABASE_URL", "SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)

    from rainalert.config import Settings

    assert "$(whoami)" in Settings().database_url


def test_an_unreachable_host_is_explained_without_socket_advice(monkeypatch, tmp_path, capsys):
    """A TCP DSN gets the TCP diagnosis; socket directories are irrelevant noise there."""
    _env(monkeypatch, tmp_path, "postgresql+psycopg://me:pw@127.0.0.1:1/rainalert")
    assert main(["ingest"]) == 1
    err = capsys.readouterr().err
    assert "127.0.0.1:1" in err
    assert "unix socket" not in err
    assert "pw" not in err  # the password is not ours to print


def test_a_database_that_fails_mid_run_keeps_its_traceback(monkeypatch):
    """Explaining away a real fault would be worse than the wall of text."""
    import pytest
    from sqlalchemy.exc import OperationalError

    from rainalert import cli

    def explode(_args):
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    monkeypatch.setattr(cli, "build_parser", lambda: _ParserStub(explode))
    with pytest.raises(OperationalError):
        main(["ingest"])


class _ParserStub:
    def __init__(self, func):
        self.func = func

    def parse_args(self, argv=None):
        import argparse

        return argparse.Namespace(func=self.func)


def test_outbox_prints_a_link_that_can_actually_be_opened(tmp_path, monkeypatch, capsys):
    """A .eml is quoted-printable: the raw text shows `token=3D...=` split across lines.

    Copying that - which is all a headless box offers - produces a token wrong in two ways at
    once, and the failure reads as "invalid token" rather than "you mistranscribed it".
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "one.eml").write_text(
        "To: me@example.com\n"
        "Subject: Regenwarnung bestaetigen\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "Content-Transfer-Encoding: quoted-printable\n"
        "MIME-Version: 1.0\n"
        "\n"
        "Zum Bestaetigen:\n"
        "http://localhost:8000/confirm#t=3DabcDEF123456789012345678901234567890=\n"
        "TAIL\n"
    )
    monkeypatch.setenv("MAIL_OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@/unused")
    monkeypatch.setenv("SECRET_KEY", "test")

    assert main(["outbox"]) == 0
    out = capsys.readouterr().out
    assert "#t=abcDEF123456789012345678901234567890TAIL" in out
    assert "=3D" not in out  # the encoding is decoded, not echoed

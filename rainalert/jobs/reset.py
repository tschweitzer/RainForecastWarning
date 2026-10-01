"""Reset local state so a test can start from nothing.

Two kinds of local state, and they are not equally cheap to rebuild:

* **Subscriptions, alert states, events, notifications** — created in seconds by clicking through
  the signup flow again. Wipe them freely.
* **Radar cycles and their archives** — every one of these was a request to a public service that
  DWD provides for free. Throwing them away means fetching them again, and a 12 h timeline is 144
  requests. Keeping them is both faster and the polite thing to do (DESIGN.md §4.3), which is why
  ``keep_radar`` defaults to true.

There is a real hazard here: this reads the same ``DATABASE_URL`` the service does, and that could
point anywhere. It refuses to touch a database that is not obviously local.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}

#: Socket directories that are never local, however much the URL looks like it.
#:
#: The empty hostname in LOCAL_HOSTS is there for a unix socket, which a DSN writes as
#: `postgresql://user:pw@/dbname?host=/some/socket/dir` - no host component at all. That is how a
#: local Postgres is reached, and it is also exactly how Cloud Run reaches **production**: the
#: Cloud SQL Auth proxy listens on `/cloudsql/<project>:<region>:<instance>` (see
#: `local.socket` in infra/secrets.tf). So the empty host matched, `assert_local` returned, and
#: `rainalert reset` would have truncated every subscriber table in the live database.
CLOUD_SOCKET_PREFIXES = ("/cloudsql/",)

#: Everything except the radar tables, in an order foreign keys allow.
SUBSCRIBER_TABLES = (
    "notifications",
    "rain_events",
    "evaluations",
    "alert_states",
    "auth_tokens",
    "subscriptions",
    "subscribers",
    "rate_limit_hits",
)
RADAR_TABLES = ("radar_cycles",)


class NotLocal(RuntimeError):
    """The database does not look local, so this refuses to drop anything."""


@dataclass
class ResetReport:
    tables_cleared: tuple[str, ...] = ()
    directories_cleared: tuple[str, ...] = ()
    radar_kept: bool = True


def assert_local(database_url: str) -> None:
    """Refuse anything that is not plainly a local database.

    A unix socket (``host=/tmp/...``) or localhost is local. A hostname is not. This is the only
    thing standing between "reset my test data" and a very bad afternoon - which is why the socket
    case is checked rather than assumed.

    It was assumed, and the assumption was wrong in the worst available direction. A DSN with a
    socket has no host component, the empty string was in ``LOCAL_HOSTS``, and the production URL
    this service runs on is exactly that shape: ``@/rainalert?host=/cloudsql/...``, because Cloud Run
    reaches Cloud SQL through the Auth proxy's unix socket. So this function answered "local" for the
    live database, and the one guard on a TRUNCATE of every subscriber table did nothing at all.
    Nothing reached it - there is no reset job in infra/ - but a guard that is trusted and wrong is
    worse than no guard.
    """
    parsed = urlparse(database_url)
    host = (parsed.hostname or "").lower()

    # Checked before the host, because this is the case a bare host check cannot see. `urlparse`
    # leaves the socket path in the query string, so read it from there rather than from `host`.
    socket = parse_qs(parsed.query).get("host", [""])[0]
    for prefix in CLOUD_SOCKET_PREFIXES:
        if socket.startswith(prefix):
            raise NotLocal(
                f"refusing to reset a database reached through {socket!r}. That is a managed "
                "instance's socket, not a local one - this command only touches localhost or a "
                "local unix socket."
            )

    if host in LOCAL_HOSTS:
        # A DSN carrying host=/some/socket/dir parses with no hostname; that is local, unless the
        # socket is one of the managed ones refused above.
        return
    raise NotLocal(
        f"refusing to reset a database on host {host!r}. "
        "This command only touches localhost or a unix socket."
    )


def reset(
    engine: Engine,
    directories: tuple[str | Path | None, ...] = (),
    *,
    keep_radar: bool = True,
) -> ResetReport:
    """Truncate the subscriber-side tables, optionally the radar ones, and clear the directories."""
    assert_local(str(engine.url.render_as_string(hide_password=False)))

    tables = SUBSCRIBER_TABLES if keep_radar else SUBSCRIBER_TABLES + RADAR_TABLES
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        }
        present = tuple(t for t in tables if t in existing)
        if present:
            # RESTART IDENTITY so a fresh run's ids start at 1 and logs are readable.
            conn.execute(
                # Table names come from the module constant above, never from input.
                text(f"TRUNCATE {', '.join(present)} RESTART IDENTITY CASCADE")
            )

    cleared: list[str] = []
    for directory in directories:
        if not directory:
            continue
        path = Path(directory)
        if path.exists():
            shutil.rmtree(path)
            cleared.append(str(path))

    logger.info(
        "reset %d table(s)%s, cleared %d director(ies)",
        len(present),
        " (radar data kept)" if keep_radar else " including radar data",
        len(cleared),
    )
    return ResetReport(present, tuple(cleared), keep_radar)

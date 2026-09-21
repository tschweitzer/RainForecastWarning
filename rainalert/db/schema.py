"""Is the database's schema the one this code was written against?

A readiness probe that only runs `SELECT 1` answers "can I reach the database", which is not the
question that matters after a deploy. A server running new code against an un-migrated database
connects perfectly well and then fails on the first request that touches whatever the migration
added - as an opaque 500, a long way from the cause. Checking the revision turns that into one
sentence, at the only moment anyone is looking.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: `migrations/` sits beside the `rainalert` package in a checkout and beside it in the image
#: (Dockerfile: both are copied into /app). It is not package data, so a wheel install has no
#: copy - hence every path here tolerates its absence rather than assuming it.
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@lru_cache(maxsize=1)
def expected_revision() -> str | None:
    """The head revision on disk, or None if the migrations are not shipped alongside."""
    if not MIGRATIONS_DIR.is_dir():
        return None
    try:
        from alembic.script import ScriptDirectory

        heads = ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()
    except Exception as exc:  # noqa: BLE001 - never let a diagnostic break the thing it checks
        logger.warning("could not read the migration history: %s", exc)
        return None
    if len(heads) != 1:
        # Two heads means a branched history that `alembic upgrade head` cannot resolve either.
        logger.warning("migration history has %d heads: %s", len(heads), ", ".join(heads))
        return None
    return heads[0]


def current_revision(session: Session) -> str | None:
    """What the database says it is at, or None if it has never been migrated."""
    row = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    return row


def schema_complaint(session: Session) -> str | None:
    """One sentence naming the problem, or None when the schema is current.

    Returns None when it cannot tell - an unknown answer must not be reported as a failure, or
    a wheel install with no migrations directory would be permanently unready.
    """
    expected = expected_revision()
    if expected is None:
        return None
    try:
        actual = current_revision(session)
    except Exception:  # noqa: BLE001 - no alembic_version table at all is the "never run" case
        session.rollback()
        return "the database has no schema yet - run `make migrate`"
    if actual is None:
        return "the database has no schema yet - run `make migrate`"
    if actual != expected:
        return (
            f"the database is at migration {actual} but this code expects {expected} - "
            "run `make migrate`"
        )
    return None

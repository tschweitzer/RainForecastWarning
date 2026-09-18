from pathlib import Path

import pytest

from tests.helpers import FIXTURES


@pytest.fixture(autouse=True)
def _fresh_settings():
    """`get_settings` is lru_cached - one Settings per process, which is right in production.

    In a test run it means the first test to call it fixes the configuration for every test
    after, so a later test reads another test's environment and passes or fails for reasons that
    have nothing to do with it. Clear it around each test.
    """
    from rainalert.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def wet_cycle() -> Path:
    """2026-09-16 13:55 UTC - a rainy cycle, frames _000/_060/_120."""
    return FIXTURES / "DE1200_RV2609161355_trimmed.tar.bz2"


@pytest.fixture(scope="session")
def outage_cycles() -> Path:
    """2026-09-15 16:15-16:30 - the Borkum radar dropout, frames _000/_005/_060 of four cycles."""
    return FIXTURES / "DE1200_RV_outage_20260915_1615-1630.tar.bz2"


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """A real Postgres. The advisory lock and the unique-constraint behaviour are dialect
    specific, so testing them on SQLite would prove nothing about production."""
    import os

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        # Skipping is right on a laptop with no Postgres, but in CI it would turn a broken
        # service container into a green build. There, the skip has to be a failure instead.
        if os.environ.get("REQUIRE_DATABASE_TESTS"):
            pytest.fail("REQUIRE_DATABASE_TESTS is set but TEST_DATABASE_URL is not")
        pytest.skip("TEST_DATABASE_URL not set")
    return url


@pytest.fixture()
def db(postgres_url):
    from sqlalchemy import text

    from rainalert.db.session import create_all, make_engine, make_session_factory

    engine = make_engine(postgres_url)
    # Reset the whole schema rather than dropping tables by name: with foreign keys between them
    # the drop order matters, and enum types outlive their tables.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    create_all(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()

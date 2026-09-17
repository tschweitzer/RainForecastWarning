from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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

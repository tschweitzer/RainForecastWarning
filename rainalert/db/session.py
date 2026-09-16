"""Engine, sessions, and the pipeline lock."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from rainalert.db.models import Base

#: Arbitrary but fixed: the advisory lock id for the ingest pipeline.
INGEST_LOCK_ID = 0x7261696E  # "rain"


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True)


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextlib.contextmanager
def pipeline_lock(session: Session) -> Iterator[bool]:
    """Hold the ingest advisory lock, or yield False if another run holds it.

    Cloud Run jobs are at-least-once, so two executions can overlap. Without this, a retried run
    can duplicate work; combined with the ``nominal_time`` unique constraint it means a cycle is
    processed exactly once. Non-blocking on purpose: if another run has it, this run should exit,
    not queue up behind it and then do redundant work five minutes late.
    """
    if session.bind.dialect.name != "postgresql":  # pragma: no cover - dev fallback
        yield True
        return
    acquired = bool(
        session.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": INGEST_LOCK_ID}).scalar()
    )
    try:
        yield acquired
    finally:
        if acquired:
            session.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": INGEST_LOCK_ID})
            session.commit()

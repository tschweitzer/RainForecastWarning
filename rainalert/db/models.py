"""Persistent model.

Only ``radar_cycles`` exists at M2 - the subscription tables arrive with M3. The schema is created
with ``metadata.create_all`` for now; Alembic comes in with M3, when there is more than one table
and migrations start to matter. That is a deliberate deferral, not an oversight.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CycleStatus(enum.StrEnum):
    #: Fetched, decoded, complete and plausible.
    OK = "ok"
    #: Fetched and stored, but incomplete or implausible - must not be evaluated as truth.
    PARTIAL = "partial"
    #: Refused before storage (bad size, bad header, timestamp outside the accepted window).
    REJECTED = "rejected"
    #: The fetch itself failed.
    FAILED = "failed"


class RadarCycle(Base):
    __tablename__ = "radar_cycles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: From the file header, validated against our own clock (§4.3.1 rule 5) - never from the URL,
    #: because _LATEST carries no timestamp. Unique: this is what makes a retried job idempotent.
    nominal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32))
    bytes: Mapped[int] = mapped_column(Integer)
    frame_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[CycleStatus] = mapped_column(
        Enum(CycleStatus, name="cycle_status", values_callable=lambda e: [m.value for m in e])
    )
    archive_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why a cycle is partial/rejected, so a gap in the timeline has an explanation (§4.3 rule 7).
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

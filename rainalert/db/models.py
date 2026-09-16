"""Persistent model. Schema changes go through Alembic (``alembic revision --autogenerate``)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    Time,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class SubscriptionStatus(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    #: Evaluation keeps failing for this subscription - surfaced to the user rather than
    #: silently never warning them (SECURITY_REVIEW.md F-3).
    UNHEALTHY = "unhealthy"


class TokenPurpose(enum.StrEnum):
    CONFIRM = "confirm"
    API = "api"
    UNSUBSCRIBE = "unsubscribe"


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    #: Stored lowercased. Kept in plaintext because we must be able to send mail to it; the hash
    #: exists so lookups and rate limiting never need to match on the address itself.
    email: Mapped[str] = mapped_column(String(320))
    email_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, index=True)
    locale: Mapped[str] = mapped_column(String(8), default="de")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # --- consent record (GDPR Art. 7(1): we must be able to demonstrate consent) ---
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Salted hash. An unsalted hash of an IPv4 address is trivially reversible - the whole space
    #: is 2^32 (SECURITY_REVIEW.md F-12).
    consent_ip_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    consent_user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: Which wording they agreed to, so the record still means something after the text changes.
    consent_text_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="subscriber", cascade="all, delete-orphan"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        # Belt and braces with the API-level validation: a location that cannot be evaluated is a
        # permanent outage for everyone if the loop is not isolated, so it must not be storable.
        CheckConstraint("lat BETWEEN 47.0 AND 56.0", name="lat_in_germany"),
        CheckConstraint("lon BETWEEN 5.0 AND 16.0", name="lon_in_germany"),
        CheckConstraint("radius_m BETWEEN 0 AND 20000", name="radius_sane"),
        CheckConstraint("threshold_mm_5min > 0", name="threshold_positive"),
        CheckConstraint("lead_time_minutes BETWEEN 5 AND 120", name="lead_sane"),
        CheckConstraint("min_gap_minutes >= 0", name="min_gap_sane"),
        Index(
            "one_subscription_per_subscriber",
            "subscriber_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscribers.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(
            SubscriptionStatus,
            name="subscription_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SubscriptionStatus.PENDING,
    )

    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    location_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grid_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grid_col: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Alert rule (D-14): per-subscription columns with the D-13 defaults. Not exposed in the v1 UI.
    radius_m: Mapped[int] = mapped_column(Integer, default=2000)
    threshold_mm_5min: Mapped[float] = mapped_column(Numeric(5, 2), default=0.10)
    lead_time_minutes: Mapped[int] = mapped_column(Integer, default=30)

    # Throttling (D-9/D-10): present, disabled by default.
    min_gap_minutes: Mapped[int] = mapped_column(Integer, default=0)
    quiet_hours_start: Mapped[object | None] = mapped_column(Time, nullable=True)
    quiet_hours_end: Mapped[object | None] = mapped_column(Time, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Berlin")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Why the subscription is unhealthy, shown to the user.
    health_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscriber: Mapped[Subscriber] = relationship(back_populates="subscriptions")


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscribers.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[TokenPurpose] = mapped_column(
        Enum(TokenPurpose, name="token_purpose", values_callable=lambda e: [m.value for m in e])
    )
    #: Only ever the hash. The plaintext exists for the length of one request and one email.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RateLimitHit(Base):
    """Abuse counters, keyed by an opaque bucket.

    Deliberately *not* cascaded from subscribers: deleting an account must erase the account, not
    the evidence that it was used to hammer the subscribe endpoint (SECURITY_REVIEW.md F-5).
    """

    __tablename__ = "rate_limit_hits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bucket: Mapped[str] = mapped_column(String(128), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

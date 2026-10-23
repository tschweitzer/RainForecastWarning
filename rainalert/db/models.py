"""Persistent model. Schema changes go through Alembic (``alembic revision --autogenerate``)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
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

#: Column widths for the two RFC 8291 keys, named because the request model caps its fields at
#: exactly these values. A browser produces 87 and 22 characters; the headroom is for a padded or
#: otherwise longer encoding of the same key, not for an arbitrary string.
#:
#: They are constants rather than two literals in two files because they were two literals in two
#: files, and the pair disagreed: the validator allowed 256 and 128 into columns of 128 and 64, so
#: an over-long key became a `DataError` on flush - a 500 from an unauthenticated endpoint. Widening
#: either of these means a migration; `test_push_key_bounds_match_the_columns` fails if only one
#: side moves.
PUSH_P256DH_MAX_LENGTH = 128
PUSH_AUTH_MAX_LENGTH = 64


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
    #: Percent of the radar area wet at t+0 (D-70), for the next cycle's plausibility check.
    wet_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Radars DWD reported as missing from this cycle, e.g. ["dehnr"] (D-75). NULL when the
    #: edition did not say - the binary one never did.
    missing_sites: Mapped[list[str] | None] = mapped_column(ARRAY(String(16)), nullable=True)


class SubscriptionStatus(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    #: Evaluation keeps failing for this subscription - surfaced to the user rather than
    #: silently never warning them (SECURITY_REVIEW.md F-3).
    UNHEALTHY = "unhealthy"


class TokenPurpose(enum.StrEnum):
    CONFIRM = "confirm"
    # 'api' - the long-lived bearer token, email subscribers only - was removed with email (D-76);
    # the value stays in the database type, and no row carries it (migration e5b2c8d4f1a6).
    UNSUBSCRIBE = "unsubscribe"
    #: The magic link to the settings page. Stored rather than signed, because it has to be
    #: single use: a signed link stays valid until it expires however often it is opened, and a
    #: link that reaches someone's home coordinates should stop working once it has been used.
    MANAGE = "manage"


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # `subscribers.channel` still exists in the database, defaulting to 'WEBPUSH', and is no longer
    # mapped: every subscriber is a web push subscriber since D-76 (migration e5b2c8d4f1a6). It is
    # kept only so the deploy that removed email could run its migration under the previous code,
    # which still read it; any later release may drop it. Raw SQL against it must use uppercase
    # (`tests/test_packaging.py` checks): it stores enum names, and a lowercase match once left
    # rows behind that crashed the alerting cycle (a9e4d2c71f05).
    #: The browser's push endpoint URL. Kept in plaintext because we must be able to reach it; the
    #: hash exists so lookups and rate limiting never match on the address itself.
    #:
    #: `Text`: a push endpoint has no length bound. Measured 87-250 characters across FCM, Mozilla
    #: autopush and Apple, but nothing in the Push API promises a ceiling, and a silently truncated
    #: endpoint is a subscriber who can never be reached again.
    address: Mapped[str] = mapped_column(Text)
    #: `tokens.hash_address(address)`. Its input still carries the `webpush:` prefix it had when
    #: there were two channels, so every stored hash stays valid.
    address_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, index=True)

    # --- web push keys (RFC 8291) -------------------------------------------------------------
    #: The browser's P-256 public key, base64url without padding - 87 characters in practice.
    #: Half of what the payload is encrypted to; without it a notification cannot be built at
    #: all, which is why a webpush subscriber with either of these NULL is a broken row rather
    #: than a degraded one.
    push_p256dh: Mapped[str | None] = mapped_column(String(PUSH_P256DH_MAX_LENGTH), nullable=True)
    #: The 16-byte shared authentication secret, base64url - 22 characters. Named `auth` in the
    #: browser's own `PushSubscription.getKey()` output.
    push_auth: Mapped[str | None] = mapped_column(String(PUSH_AUTH_MAX_LENGTH), nullable=True)
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

    #: When a person last demonstrably saw something from us: tapped a warning (its `#l=` link
    #: reaches `POST /api/v1/locate`) or opened their settings. The liveness job's "acted on
    #: nothing" signal (`count_silent_subscribers`), which used to be "pressed the Einstellungen
    #: button" until notifications stopped carrying buttons (DESIGN.md D-64). Written at most once
    #: a day per subscriber, so it costs no write per request.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    #: 0.15 rather than a rounder 0.10 so that a new subscription lands on a named band of
    #: the intensity scale (§11.1.1, "leichter Regen") instead of between two of them, which
    #: the settings page can only show as "eigener Wert".
    #: Three decimals since D-80: "Nieselregen" starts at 0.008 mm (migration f7c4a1e9b3d2).
    threshold_mm_5min: Mapped[float] = mapped_column(Numeric(6, 3), default=0.15)
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
    #: Only ever the hash. The plaintext exists for the length of one request and one message.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceKey(Base):
    """The key a push subscriber's browser signs its settings requests with (DESIGN.md D-64).

    Only the public half is here; the private half is a non-extractable WebCrypto key in that
    browser's IndexedDB and never leaves it. Registered only together with a push-delivered
    single-use token redeemed in the browser that holds the subscription, so the key is exactly as
    strong as the push round trip it replaces (docs/PLAN_DEVICE_KEY.md §4.1).

    One per subscriber - a push subscriber is one browser - so registering or rotating replaces
    the row. Deleted with the subscriber by the database's cascade, like `auth_tokens`.
    """

    __tablename__ = "device_keys"

    #: `base64url(SHA-256(SPKI))` without padding: 43 characters, computed by the browser and by
    #: us alike, so the confirm page - a form POST with no script on the way back - needs no answer
    #: to learn it.
    id: Mapped[str] = mapped_column(String(43), primary_key=True)
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscribers.id", ondelete="CASCADE"), unique=True
    )
    #: SubjectPublicKeyInfo DER of a P-256 key, 91 octets. Public; not a secret.
    public_key: Mapped[bytes] = mapped_column(LargeBinary(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: At most one write a day (see `Subscriber.last_seen_at`).
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


class AlertState(enum.StrEnum):
    #: No trustworthy observation yet - a fresh subscription, or the location just moved.
    UNKNOWN = "unknown"
    DRY = "dry"
    #: Rain is coming and we have said so. Exactly one notification per event comes from this edge.
    WARNED = "warned"
    RAINING = "raining"


class Evaluation(Base):
    """One subscription's view of one cycle.

    A rolling debug log, not a permanent record (D-23). Everything here can be recomputed from the
    archived grid *except* the decision, and the decision is what the durable tables below keep.
    """

    __tablename__ = "evaluations"
    __table_args__ = (
        Index("uq_evaluation", "subscription_id", "cycle_id", unique=True),
        Index("evaluations_by_sub_time", "subscription_id", "evaluated_at"),
        Index("evaluations_purge", "evaluated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE")
    )
    cycle_id: Mapped[int] = mapped_column(ForeignKey("radar_cycles.id", ondelete="CASCADE"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    now_wet: Mapped[bool] = mapped_column(Boolean)
    first_hit_lead_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 25 slots indexed by lead/5 - NOT by frame position (§8.0). NULL where that lead is absent.
    max_rate_by_lead: Mapped[list[float | None]] = mapped_column(ARRAY(Float))
    missing_fraction: Mapped[list[float | None]] = mapped_column(ARRAY(Float))
    state_before: Mapped[AlertState] = mapped_column(
        Enum(AlertState, name="alert_state", values_callable=lambda e: [m.value for m in e])
    )
    state_after: Mapped[AlertState] = mapped_column(
        Enum(AlertState, name="alert_state", values_callable=lambda e: [m.value for m in e])
    )
    decision: Mapped[str] = mapped_column(String(32))


class SubscriptionAlertState(Base):
    """The live state machine position. One row per subscription."""

    __tablename__ = "alert_states"

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[AlertState] = mapped_column(
        Enum(AlertState, name="alert_state", values_callable=lambda e: [m.value for m in e]),
        default=AlertState.UNKNOWN,
    )
    state_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Which location this state reflects. Compared against Subscription.location_updated_at to
    #: detect a move (D-17). A dedicated field rather than reusing state_since, because the two
    #: timestamps come from different clocks - the API's wall clock and the evaluator's - and
    #: comparing across them makes the reset fire either never or every cycle.
    location_applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: When t+0 was last dry, for the RAINING -> DRY timer.
    dry_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Consecutive cycles with no hit while WARNED, for the retraction rule.
    no_hit_cycles: Mapped[int] = mapped_column(Integer, default=0)
    current_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RainEvent(Base):
    """One rain event per subscription. Permanent (D-23) - this is the record of what we said."""

    __tablename__ = "rain_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    predicted_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    peak_mm_5min: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: True when the rain arrived within the tolerance, False when it did not, NULL until judged.
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Notification(Base):
    """Queued before delivery, in the same transaction as the state change.

    A crash between commit and send is recovered by the next cycle; a notification that is no
    longer true is expired rather than sent, because a late rain warning is worse than none.
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("notifications_pending", "status", "queued_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("rain_events.id", ondelete="SET NULL"), nullable=True
    )
    # `notifications.channel` remains in the database with default 'webpush', unmapped (D-76).
    status: Mapped[str] = mapped_column(String(16), default="queued")
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)

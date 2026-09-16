"""Subscription lifecycle: subscribe, confirm, update, delete.

All the state transitions live here rather than in the HTTP layer, so they can be tested without a
web server and reused by the future mobile app unchanged.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import available_timezones

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from rainalert.config import Settings
from rainalert.db.models import (
    AuthToken,
    Subscriber,
    Subscription,
    SubscriptionStatus,
    TokenPurpose,
)
from rainalert.radar.grid import OutsideGrid, cell_of
from rainalert.tokens import expiry, hash_email, hash_ip, hash_token, new_token

#: Germany plus a margin, matching the CHECK constraints. Deliberately not the whole planet: a
#: location that cannot be evaluated is worse than a rejected one (SECURITY_REVIEW.md F-3).
LAT_RANGE = (47.0, 56.0)
LON_RANGE = (5.0, 16.0)

_TIMEZONES = available_timezones()


class ValidationError(ValueError):
    """The request cannot be stored. Safe to show to the user."""


@dataclass
class SubscribeResult:
    """What the caller may learn.

    Note what is *not* here: whether the address was already known. The endpoint answers
    identically either way, so it cannot be used to test whether someone is subscribed.
    """

    confirm_token: str | None
    subscriber_id: uuid.UUID | None
    already_active: bool


def validate_location(lat: float, lon: float) -> tuple[float, float]:
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        raise ValidationError("latitude and longitude must be numbers")
    lat, lon = float(lat), float(lon)
    # NaN and infinities arrive through the API - json.loads accepts the bare tokens - and NaN
    # fails every comparison, so the range check alone would reject it silently. Be explicit.
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValidationError("latitude and longitude must be finite")
    if not LAT_RANGE[0] <= lat <= LAT_RANGE[1] or not LON_RANGE[0] <= lon <= LON_RANGE[1]:
        raise ValidationError("the service currently covers Germany only")
    try:
        cell_of(lat, lon)
    except OutsideGrid as exc:
        raise ValidationError("that location is outside the radar grid") from exc
    return lat, lon


def validate_timezone(name: str) -> str:
    if name not in _TIMEZONES:
        raise ValidationError(f"unknown timezone {name!r}")
    return name


def subscribe(
    session: Session,
    settings: Settings,
    *,
    email: str,
    lat: float,
    lon: float,
    client_ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> SubscribeResult:
    """Start a double opt-in. Returns the confirmation token for the caller to mail.

    Nothing is ever sent to an address that has not confirmed, which is what stops this endpoint
    being usable as a mail relay or to bomb a third party.
    """
    now = now or datetime.now(UTC)
    email = email.strip().lower()
    lat, lon = validate_location(lat, lon)

    digest = hash_email(email)
    subscriber = session.execute(
        select(Subscriber).where(Subscriber.email_hash == digest)
    ).scalar_one_or_none()

    if subscriber and subscriber.confirmed_at:
        # Already confirmed. Do not re-issue a token and do not tell the caller anything.
        return SubscribeResult(None, None, already_active=True)

    if subscriber is None:
        subscriber = Subscriber(
            email=email,
            email_hash=digest,
            created_at=now,
            consent_ip_hash=hash_ip(client_ip, settings.secret_key) if client_ip else None,
            consent_user_agent=(user_agent or "")[:256] or None,
            consent_text_version=settings.consent_text_version,
        )
        session.add(subscriber)
        session.flush()

    subscription = session.execute(
        select(Subscription).where(Subscription.subscriber_id == subscriber.id)
    ).scalar_one_or_none()
    if subscription is None:
        subscription = Subscription(
            subscriber_id=subscriber.id,
            status=SubscriptionStatus.PENDING,
            lat=lat,
            lon=lon,
            location_updated_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(subscription)
    else:
        subscription.lat, subscription.lon = lat, lon
        subscription.location_updated_at = now
        subscription.updated_at = now

    # Supersede any previous unused confirmation token: a resend must not leave two live tokens.
    session.execute(
        delete(AuthToken).where(
            AuthToken.subscriber_id == subscriber.id,
            AuthToken.purpose == TokenPurpose.CONFIRM,
            AuthToken.used_at.is_(None),
        )
    )
    token = new_token()
    session.add(
        AuthToken(
            subscriber_id=subscriber.id,
            purpose=TokenPurpose.CONFIRM,
            token_hash=hash_token(token),
            expires_at=expiry(settings.confirm_token_ttl_hours, now),
            created_at=now,
        )
    )
    session.commit()
    return SubscribeResult(token, subscriber.id, already_active=False)


@dataclass
class ConfirmResult:
    api_token: str
    unsubscribe_token: str
    subscriber_id: uuid.UUID


def confirm(
    session: Session, settings: Settings, *, token: str, now: datetime | None = None
) -> ConfirmResult:
    """Consume a confirmation token and activate the subscription.

    Single use: a mail scanner that follows the link would otherwise consume it and the real user
    would be told it is already used (SECURITY_REVIEW.md F-4). That is why the HTTP layer only
    calls this from a POST.
    """
    now = now or datetime.now(UTC)
    row = session.execute(
        select(AuthToken).where(
            AuthToken.token_hash == hash_token(token),
            AuthToken.purpose == TokenPurpose.CONFIRM,
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        raise ValidationError("this confirmation link is not valid")
    if row.expires_at and row.expires_at < now:
        raise ValidationError("this confirmation link has expired")

    row.used_at = now
    subscriber = session.get(Subscriber, row.subscriber_id)
    if subscriber is None:
        raise ValidationError("this confirmation link is not valid")
    subscriber.confirmed_at = subscriber.confirmed_at or now

    subscription = session.execute(
        select(Subscription).where(Subscription.subscriber_id == subscriber.id)
    ).scalar_one()
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.updated_at = now

    api_token, unsub_token = new_token(), new_token()
    for value, purpose in ((api_token, TokenPurpose.API), (unsub_token, TokenPurpose.UNSUBSCRIBE)):
        session.add(
            AuthToken(
                subscriber_id=subscriber.id,
                purpose=purpose,
                token_hash=hash_token(value),
                expires_at=None,
                created_at=now,
            )
        )
    session.commit()
    return ConfirmResult(api_token, unsub_token, subscriber.id)


def resolve_token(
    session: Session, *, token: str, purpose: TokenPurpose, now: datetime | None = None
) -> Subscriber:
    now = now or datetime.now(UTC)
    row = session.execute(
        select(AuthToken).where(
            AuthToken.token_hash == hash_token(token), AuthToken.purpose == purpose
        )
    ).scalar_one_or_none()
    if row is None or (row.expires_at and row.expires_at < now):
        raise ValidationError("not authorised")
    row.last_used_at = now
    subscriber = session.get(Subscriber, row.subscriber_id)
    if subscriber is None:
        raise ValidationError("not authorised")
    return subscriber


def update_location(
    session: Session,
    subscriber: Subscriber,
    *,
    lat: float,
    lon: float,
    now: datetime | None = None,
) -> Subscription:
    """Move the location. Resets alert state when the move is significant (D-17).

    Without that reset, driving into rain that is already falling produces a "rain starting"
    warning for rain you are already in.
    """
    from rainalert.radar.grid import _GEOD  # local import: geodesy is not needed to import this

    now = now or datetime.now(UTC)
    lat, lon = validate_location(lat, lon)
    subscription = session.execute(
        select(Subscription).where(Subscription.subscriber_id == subscriber.id)
    ).scalar_one()

    _, _, moved_m = _GEOD.inv(subscription.lon, subscription.lat, lon, lat)
    subscription.lat, subscription.lon = lat, lon
    subscription.location_updated_at = now
    subscription.updated_at = now
    subscription.grid_row = subscription.grid_col = None  # cached cell is stale
    if moved_m > 1000:
        subscription.health_note = None
        # Alert state lives in M4's table; the flag it keys off is the location timestamp.
    session.commit()
    return subscription


def delete_subscriber(session: Session, subscriber: Subscriber) -> None:
    """Hard delete: subscriber, subscription and tokens. Rate-limit rows deliberately survive."""
    session.delete(subscriber)
    session.commit()


def purge_unconfirmed(session: Session, settings: Settings, now: datetime | None = None) -> int:
    """Delete never-confirmed signups. Holding them indefinitely is not minimisation."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=settings.unconfirmed_purge_hours)
    stale = (
        session.execute(
            select(Subscriber).where(
                Subscriber.confirmed_at.is_(None), Subscriber.created_at < cutoff
            )
        )
        .scalars()
        .all()
    )
    for subscriber in stale:
        session.delete(subscriber)
    session.commit()
    return len(stale)

"""Subscription lifecycle: subscribe, confirm, update, delete.

All the state transitions live here rather than in the HTTP layer, so they can be tested without a
web server and reused by the future mobile app unchanged.
"""

from __future__ import annotations

import math
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import available_timezones

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from rainalert.config import Settings
from rainalert.db.models import (
    AuthToken,
    Channel,
    Subscriber,
    Subscription,
    SubscriptionStatus,
    TokenPurpose,
)
from rainalert.radar.grid import OutsideGrid, cell_of
from rainalert.tokens import (
    expiry,
    hash_address,
    hash_ip,
    hash_token,
    new_token,
    unsubscribe_token,
)

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
    #: The address the confirmation goes to. For email it is what the caller supplied; for ntfy
    #: it is the topic generated here, which the caller has to show the subscriber because
    #: nothing can reach them until their app is subscribed to it.
    address: str | None = None


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


#: 128 bits, url-safe. Enough that an ntfy topic cannot be found by trying.
NTFY_TOPIC_BYTES = 16


def new_ntfy_topic(prefix: str = "rainalert") -> str:
    """A push topic nobody can guess.

    Topics on a public ntfy server are a flat, unauthenticated namespace: anyone who knows a
    topic can subscribe to it, and a rain warning says where and when it will rain for the person
    who gets it. A topic anyone can guess is therefore a location leak, which is why this is
    generated rather than chosen - `rainalert-muenchen` would be readable, memorable, and someone
    else's within a week.
    """
    return f"{prefix}-{secrets.token_urlsafe(NTFY_TOPIC_BYTES)}"


def subscribe(
    session: Session,
    settings: Settings,
    *,
    lat: float,
    lon: float,
    channel: Channel = Channel.EMAIL,
    address: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> SubscribeResult:
    """Start a double opt-in. Returns the confirmation token for the caller to deliver.

    Nothing is ever sent to an address that has not confirmed. For email that is what stops this
    endpoint being usable as a mail relay or to bomb a third party. For a push topic there is no
    third party to protect - the topic did not exist until now - but the confirmation earns its
    place for a different reason: it proves the channel actually reaches the subscriber. A rain
    warning that silently goes nowhere is worse than none, because they stop watching the sky.

    ``address`` is required for email and ignored for ntfy, where the topic is generated here.
    """
    now = now or datetime.now(UTC)
    lat, lon = validate_location(lat, lon)

    if channel == Channel.EMAIL:
        if not address:
            raise ValidationError("an email address is required")
        address = address.strip().lower()
    else:
        # Never taken from the request: see new_ntfy_topic.
        address = new_ntfy_topic(settings.ntfy_topic_prefix)

    digest = hash_address(channel.value, address)
    subscriber = session.execute(
        select(Subscriber).where(Subscriber.address_hash == digest)
    ).scalar_one_or_none()

    if subscriber and subscriber.confirmed_at:
        # Already confirmed. Do not re-issue a token and do not tell the caller anything.
        return SubscribeResult(None, None, already_active=True)

    if subscriber is None:
        subscriber = Subscriber(
            channel=channel,
            address=address,
            address_hash=digest,
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
    return SubscribeResult(token, subscriber.id, already_active=False, address=address)


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

    api_token = new_token()
    session.add(
        AuthToken(
            subscriber_id=subscriber.id,
            purpose=TokenPurpose.API,
            token_hash=hash_token(api_token),
            expires_at=None,
            created_at=now,
        )
    )
    session.commit()
    # Signed rather than stored, so every future alert mail can carry a working link (tokens.py).
    return ConfirmResult(
        api_token, unsubscribe_token(subscriber.id, settings.secret_key), subscriber.id
    )


def issue_manage_token(
    session: Session, settings: Settings, subscriber: Subscriber, now: datetime | None = None
) -> str:
    """Mint the single-use link that opens the settings page.

    Any previous unused one is superseded, so asking for a second link does not leave the first
    working: two live links to someone's home coordinates is one more than was asked for.
    """
    now = now or datetime.now(UTC)
    session.execute(
        delete(AuthToken).where(
            AuthToken.subscriber_id == subscriber.id,
            AuthToken.purpose == TokenPurpose.MANAGE,
            AuthToken.used_at.is_(None),
        )
    )
    token = new_token()
    session.add(
        AuthToken(
            subscriber_id=subscriber.id,
            purpose=TokenPurpose.MANAGE,
            token_hash=hash_token(token),
            expires_at=now + timedelta(minutes=settings.manage_link_ttl_minutes),
            created_at=now,
        )
    )
    session.commit()
    return token


def find_subscriber(session: Session, *, channel: Channel, address: str) -> Subscriber | None:
    """Look a subscriber up by what they would type. Returns None rather than raising.

    The caller must answer identically whether this finds anything or not: a settings page that
    says "unknown address" is an oracle for who has signed up, and the addresses are mailboxes
    and push topics belonging to real people.
    """
    return session.execute(
        select(Subscriber).where(Subscriber.address_hash == hash_address(channel.value, address))
    ).scalar_one_or_none()


def redeem_manage_token(session: Session, *, token: str, now: datetime | None = None) -> Subscriber:
    """Spend the magic link. Single use: the second attempt fails like a wrong token."""
    now = now or datetime.now(UTC)
    row = session.execute(
        select(AuthToken).where(
            AuthToken.token_hash == hash_token(token),
            AuthToken.purpose == TokenPurpose.MANAGE,
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or (row.expires_at and row.expires_at < now):
        raise ValidationError("this link is no longer valid")
    row.used_at = now
    subscriber = session.get(Subscriber, row.subscriber_id)
    if subscriber is None:
        raise ValidationError("this link is no longer valid")
    session.commit()
    return subscriber


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


#: RV carries a frame every five minutes, and rules.py walks the leads in the same step. A lead
#: time that is not a multiple of it is rounded down at evaluation time, silently.
LEAD_STEP_MINUTES = 5


def validate_rule(
    settings: Settings,
    *,
    threshold_mm_5min: float | None = None,
    lead_time_minutes: int | None = None,
    radius_m: int | None = None,
) -> dict:
    """Check the three rule values a subscriber may set, and return the ones that were given.

    Every bound here also exists as a CHECK constraint, and that is deliberate - but a constraint
    violation surfaces as an IntegrityError, which is a 500. These checks exist so that a value a
    person can type becomes a sentence they can read.
    """
    changes: dict = {}

    if threshold_mm_5min is not None:
        value = float(threshold_mm_5min)
        if not math.isfinite(value):
            raise ValidationError("the threshold must be a number")
        # The column is numeric(5,2), so a third decimal is rounded away by the database, and
        # 0.001 rounds to 0.00 - which then fails the `threshold_positive` CHECK as a 500. Round
        # here, where it can still be judged, and compare the rounded value.
        value = round(value, 2)
        floor, ceiling = settings.min_threshold_mm_5min, settings.plausibility_max_mm_5min
        if not floor <= value <= ceiling:
            raise ValidationError(
                f"the threshold must be between {floor} and {ceiling} mm per 5 minutes"
            )
        changes["threshold_mm_5min"] = value

    if lead_time_minutes is not None:
        value = int(lead_time_minutes)
        if not settings.min_lead_minutes <= value <= settings.max_lead_minutes:
            raise ValidationError(
                f"the lead time must be between {settings.min_lead_minutes} and "
                f"{settings.max_lead_minutes} minutes"
            )
        if value % LEAD_STEP_MINUTES:
            # rules.py walks the leads in steps of five, so 32 would be evaluated as 30 and the
            # stored number would be a promise the evaluation does not keep.
            raise ValidationError(
                f"the lead time must be a multiple of {LEAD_STEP_MINUTES} minutes"
            )
        changes["lead_time_minutes"] = value

    if radius_m is not None:
        value = int(radius_m)
        if not 0 <= value <= settings.max_radius_m:
            raise ValidationError(f"the radius must be between 0 and {settings.max_radius_m} m")
        changes["radius_m"] = value

    return changes


def update_rule(
    session: Session,
    settings: Settings,
    subscriber: Subscriber,
    *,
    threshold_mm_5min: float | None = None,
    lead_time_minutes: int | None = None,
    radius_m: int | None = None,
    now: datetime | None = None,
) -> Subscription:
    """Change what counts as rain worth warning about.

    Unlike a location change (D-17) this does **not** reset the alert state, and that asymmetry is
    intentional. Moving invalidates what the state machine knows, because the state describes a
    place. Changing the threshold does not: it describes what to do with what is already known,
    and resetting on every adjustment would mean a subscriber who is currently being rained on
    could re-arm their own "rain is starting" warning by nudging a number.
    """
    changes = validate_rule(
        settings,
        threshold_mm_5min=threshold_mm_5min,
        lead_time_minutes=lead_time_minutes,
        radius_m=radius_m,
    )
    subscription = session.execute(
        select(Subscription).where(Subscription.subscriber_id == subscriber.id)
    ).scalar_one()
    if not changes:
        return subscription

    for field, value in changes.items():
        setattr(subscription, field, value)
    subscription.updated_at = now or datetime.now(UTC)
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

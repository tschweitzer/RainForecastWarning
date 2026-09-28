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
    Channel,
    Subscriber,
    Subscription,
    SubscriptionStatus,
    TokenPurpose,
)
from rainalert.notify.webpush import EndpointRefused, check_endpoint
from rainalert.radar.grid import OutsideGrid, cell_of
from rainalert.tokens import (
    expiry,
    hash_address,
    hash_ip,
    hash_token,
    new_token,
    same_secret,
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
    #: The address the confirmation goes to: the mailbox for email, the push endpoint for web
    #: push. Echoed back rather than re-derived so the caller sends the confirmation to exactly
    #: what was stored - a normalised mailbox, or an endpoint that passed the host check.
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


def subscribe(
    session: Session,
    settings: Settings,
    *,
    lat: float,
    lon: float,
    channel: Channel = Channel.EMAIL,
    address: str | None = None,
    push_p256dh: str | None = None,
    push_auth: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> SubscribeResult:
    """Start a double opt-in. Returns the confirmation token for the caller to deliver.

    Nothing is ever sent to an address that has not confirmed. For email that is what stops this
    endpoint being usable as a mail relay or to bomb a third party. For web push there is no third
    party to protect - the browser handed us its own endpoint - but the confirmation keeps its
    place for the other reason: it proves the channel actually reaches the subscriber. Under web
    push there is more of that chain to get wrong, not less. A service worker that fails to
    install, a payload the browser refuses, a permission granted and then revoked before the first
    send: each leaves a subscription that looks healthy from here and shows nothing there. A rain
    warning that silently goes nowhere is worse than none, because they stop watching the sky.

    ``address`` is the mailbox for email and the push endpoint for web push; ``push_p256dh`` and
    ``push_auth`` are required with the latter and ignored otherwise.
    """
    now = now or datetime.now(UTC)
    lat, lon = validate_location(lat, lon)

    if channel == Channel.EMAIL:
        if not address:
            raise ValidationError("an email address is required")
        address = address.strip().lower()
    else:
        if not address:
            raise ValidationError("a push endpoint is required")
        if not (push_p256dh and push_auth):
            # Without both, nothing can ever be encrypted for this subscriber, so the row would be
            # dead the moment it was written. Refuse rather than store something unreachable.
            raise ValidationError("a push subscription needs its p256dh and auth keys")
        try:
            # SSRF: this endpoint is a URL chosen by whoever is calling, and the notifier will POST
            # to it. Checked here so the row never exists, and again in the notifier so a row that
            # arrived another way still cannot become a request to anywhere it likes.
            address = check_endpoint(address.strip())
        except EndpointRefused as exc:
            raise ValidationError(str(exc)) from exc

    digest = hash_address(channel.value, address)
    subscriber = session.execute(
        select(Subscriber).where(Subscriber.address_hash == digest)
    ).scalar_one_or_none()

    if subscriber and subscriber.confirmed_at:
        # Already confirmed. No second confirmation token, and - for email - nothing else either:
        # the caller only proved they can type an address, so acting on the request would let a
        # stranger move somebody else's location.
        #
        # Web push looks like the opposite case and *nearly* got the opposite treatment. It is
        # tempting to say the caller must be the browser that owns this endpoint, because that is
        # where an endpoint comes from. It is not true, and believing it reopens the hole that
        # `POST /api/v1/push/resubscribe` was deleted for on the same day (see the security table
        # and the long comment in static/sw.js): an endpoint is not a secret. It is not published,
        # but it proves nothing, because a push service will not deliver to it for anyone who lacks
        # our VAPID key. Anyone who learns one and re-POSTs it here could, for one unauthenticated
        # request:
        #   - overwrite the stored keys with their own, so every later warning is encrypted to keys
        #     the reader's browser cannot decrypt while the push service still answers 201 and this
        #     service believes it delivered - silence that neither side can see, and which the
        #     liveness job cannot catch because it measures *successful* sends;
        #   - or store an off-curve key, so the next send raises, reports `gone`, and deletes the
        #     subscriber outright;
        #   - or move the stored home coordinates.
        #
        # What actually authenticates the owning browser is the pair it already holds. `auth` is a
        # 16-byte secret the browser generated, `p256dh` its public key, and neither is published or
        # echoed anywhere - we hold them only because that browser sent them over TLS. A browser
        # re-subscribing presents the same pair, because `pushManager.subscribe()` with the same
        # applicationServerKey returns the existing subscription rather than minting a new one. An
        # attacker holding only the endpoint cannot produce them.
        #
        # So: the keys are never overwritten, and the location moves only for a caller that can
        # present them. `compare_digest` because a plain `==` on a secret is a timing oracle.
        if channel != Channel.EMAIL:
            if not (push_p256dh and push_auth):
                return SubscribeResult(None, None, already_active=False)
            # `same_secret`, not `secrets.compare_digest`: the latter raises TypeError on a
            # non-ASCII str, and these two values come straight out of a JSON body. A caller who
            # knew a confirmed endpoint and sent one umlaut in `p256dh` got an unhandled 500 out of
            # this line - the takeover fix's own contribution to the bug class it was reviewed
            # alongside. The keys are also charset-checked in `SubscribeRequest` now; this is the
            # half that holds for a row arriving by any other route.
            owner = same_secret(push_p256dh, subscriber.push_p256dh or "") and same_secret(
                push_auth, subscriber.push_auth or ""
            )
            if not owner:
                # Answered exactly as a brand-new endpoint would be, and nothing is stored or sent.
                # Identical on purpose: a different answer here would make this endpoint an oracle
                # for "is this push subscription registered?", which the rest of this module is
                # careful not to be.
                #
                # The one legitimate caller this refuses is a browser that rotated its keys while
                # keeping the same endpoint. That is not a thing browsers do - a rotation produces a
                # new subscription, so a new endpoint and a new row - and if it ever happened the
                # 410 pruning would clean up and the reader would sign up again, which is the cost
                # D-47 already states.
                return SubscribeResult(None, None, already_active=False)
            subscription = session.execute(
                select(Subscription).where(Subscription.subscriber_id == subscriber.id)
            ).scalar_one_or_none()
            if subscription is not None and (subscription.lat, subscription.lon) != (lat, lon):
                # D-17 applies: a move invalidates what the alert state believed about the old
                # place, and `update_location` is what knows that - not this function.
                update_location(session, subscriber, lat=lat, lon=lon, now=now)
        return SubscribeResult(None, None, already_active=True)

    if subscriber is None:
        subscriber = Subscriber(
            channel=channel,
            address=address,
            address_hash=digest,
            push_p256dh=push_p256dh if channel != Channel.EMAIL else None,
            push_auth=push_auth if channel != Channel.EMAIL else None,
            created_at=now,
            consent_ip_hash=hash_ip(client_ip, settings.secret_key) if client_ip else None,
            consent_user_agent=(user_agent or "")[:256] or None,
            consent_text_version=settings.consent_text_version,
        )
        session.add(subscriber)
        session.flush()
    # Deliberately no `elif` overwriting the keys of an existing unconfirmed row. A retry from the
    # same browser presents the same pair, so there is nothing to update; a request presenting a
    # different pair is someone who knows the endpoint and not the keys, and letting them replace
    # them would mean the confirmation goes out encrypted to keys the real owner cannot read - a
    # signup nobody can complete. The row keeps whatever the first request stored, and a fresh
    # confirm token is issued below to whoever can actually decrypt it.

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

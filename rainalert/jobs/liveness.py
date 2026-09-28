"""The "you are still subscribed" notification, and the deletion it exists to trigger.

D-46. This looks like a courtesy and is really a data-retention mechanism.

A web push subscriber can leave without telling us. Blocking notifications, clearing site data,
uninstalling the browser: each revokes the subscription, and none of them reaches this service. The
only way we ever find out is that a send returns 404 or 410 - so as long as we send nothing, we go
on holding somebody's home coordinates for a subscription that ended months ago. On a rainy month
the alerts discover it within days. Through a dry spell, or for someone whose threshold is never
met, nothing ever does.

So: if a subscriber has heard nothing for `webpush_liveness_days`, send one short notification. Two
things come of it, and the second is the point.

1. They are reminded the subscription exists, by something they can act on - it carries the same
   Einstellungen button as a warning, which is the route to changing or ending it.
2. If the subscription is dead, the push service says so and the row is deleted.

Email is deliberately not included. A mailbox does not revoke itself, an unsolicited periodic mail
to somebody who has not asked for one is closer to spam than to housekeeping, and the way out of
email is a link in every message that already works.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rainalert.config import Settings
from rainalert.db.models import (
    AuthToken,
    Channel,
    Notification,
    Subscriber,
    Subscription,
    SubscriptionStatus,
    TokenPurpose,
)
from rainalert.notify.base import OutboundMessage

logger = logging.getLogger(__name__)


def due_for_liveness(
    session: Session, settings: Settings, now: datetime | None = None
) -> list[tuple[Subscriber, Subscription]]:
    """Confirmed web push subscribers who have heard nothing for long enough.

    "Heard nothing" is measured from the last *successfully sent* notification, falling back to
    when they confirmed - so a subscriber who has never been warned is still covered, which is the
    case that matters most: somebody whose threshold is never met is exactly who nothing else would
    ever notice had gone.

    Deliberately a query rather than a `last_notified_at` column. The `notifications` table already
    records every send with a timestamp, and a denormalised copy would be one more thing to keep
    true - a column that drifts would mean either silence or a notification somebody did
    not need.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.webpush_liveness_days)

    last_sent = (
        select(
            Notification.subscription_id.label("subscription_id"),
            func.max(Notification.sent_at).label("last_sent_at"),
        )
        .where(Notification.sent_at.is_not(None))
        .group_by(Notification.subscription_id)
        .subquery()
    )

    rows = session.execute(
        select(Subscriber, Subscription)
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .outerjoin(last_sent, last_sent.c.subscription_id == Subscription.id)
        .where(
            Subscriber.channel == Channel.WEBPUSH,
            Subscriber.confirmed_at.is_not(None),
            # Not a paused subscription. Nothing sets PAUSED today, so this is latent rather than a
            # live bug - but the day a pause button ships, the reader who pressed it would start
            # getting a monthly notification from the service they had just told to be quiet, which
            # is the single worst message this job could send.
            Subscription.status != SubscriptionStatus.PAUSED,
            # COALESCE, not an OR: a subscriber with no sends at all has a NULL here, and
            # `NULL < cutoff` is NULL rather than true - so without this they would be skipped
            # forever, which is the exact population this job is for.
            func.coalesce(last_sent.c.last_sent_at, Subscriber.confirmed_at) < cutoff,
        )
    ).all()
    return [(subscriber, subscription) for subscriber, subscription in rows]


def liveness_message(settings: Settings, subscriber, token: str) -> OutboundMessage:
    """Short, and honest about why it arrived.

    A notification nobody asked for has to say what it is in its first line, or it reads as the
    service malfunctioning. It names the gap rather than a date, because "since 2026-08-27" invites
    the reader to work out whether that is right.
    """
    from rainalert.api.mail import settings_action

    days = settings.webpush_liveness_days
    return OutboundMessage(
        to=subscriber.address,
        channel=Channel.WEBPUSH.value,
        # Not "Regenwarnung ist aktiv", which in ordinary German reads as *a rain warning is in
        # effect* - i.e. it is about to rain. `subject` is the notification title, and a title is
        # the only part most people read on a lock screen, so the body's careful wording never got
        # a chance: the reader took an umbrella, nothing happened, and trusted the next real warning
        # less. That is the "reads as a malfunction" outcome this message was reworded to avoid,
        # reached from the other side.
        subject="Deine Regenwarnung läuft noch",
        text=(
            # Not "gab es keinen Regen zu melden", which the job cannot know: `due_for_liveness`
            # measures from the last message *we sent*, not from rainfall. Somebody with a high
            # threshold through a drizzly month would be told there was no rain, look out of the
            # window, and conclude the radar is broken - which is exactly the "reads as a
            # malfunction" outcome this wording exists to avoid, reached from the other side.
            #
            # The second sentence has to justify the message, so it says what it is *for* rather
            # than what it is not, and the third stops the reader hunting for something to do.
            # "Wir haben dir ... keine Warnung geschickt", not "mussten wir dich nicht warnen":
            # the second is still a claim about the weather, and `due_for_liveness` measures from
            # `sent_at`, so a warning that was generated and failed to deliver leaves no trace and
            # this message would assert there had been nothing to warn about. What we can state is
            # what we did.
            f"Wir haben dir seit etwa {days} Tagen keine Warnung geschickt. "
            "Diese Nachricht prüft nur, ob wir dich noch erreichen – du musst nichts tun. "
            # Derived, not hard-coded. This used to say "höchstens einmal im Monat" two sentences
            # after the line above interpolates `webpush_liveness_days`, so at any setting below 30
            # the notification contradicted itself inside three sentences.
            f"Sie kommt höchstens alle {days} Tage."
        ),
        click_url=f"{settings.public_base_url.rstrip('/')}/manage",
        actions=(settings_action(settings, token),),
        push_p256dh=subscriber.push_p256dh,
        push_auth=subscriber.push_auth,
        # The one message here that does not deserve to wake a sleeping phone, and the one that can
        # afford to wait: it is a housekeeping ping with nothing time-critical in it. A day, so a
        # phone that is off overnight still gets it and the row is not re-notified next week for
        # nothing.
        urgency="normal",
        ttl_seconds=24 * 3600,
        # No headers: this message only ever goes out over web push, where the transport sends the
        # ones RFC 8030 requires and ignores everything else.
    )


def count_silent_subscribers(session: Session) -> int:
    """Confirmed push subscribers we have successfully sent to, who have never asked for settings.

    The one failure this feature has no other instrument for: a payload encrypted to the wrong keys
    is still accepted by the push service with a 201, so we record `sent` and the reader sees
    nothing and has nothing to report. `due_for_liveness` cannot catch it either - it measures
    successful *sends*, which is exactly what a silent failure produces.

    What makes a number possible at all is that every push message carries an Einstellungen button,
    and `POST /api/v1/manage/request` is reached only when a human presses one. So a subscriber with
    successful sends and no `MANAGE` token ever issued is one who has received messages and acted on
    none of them.

    A smell, not an alarm, and it must not become one: plenty of people never need their settings.
    What matters is the trend. A count that climbs while sends keep succeeding is the signature of
    notifications being accepted and never displayed, and there was previously nothing to watch at
    all. Logged once per run so it lands in the job's output next to the send count.
    """
    sent_ok = (
        select(Notification.subscription_id)
        .where(Notification.sent_at.is_not(None))
        .scalar_subquery()
    )
    asked = select(AuthToken.subscriber_id).where(AuthToken.purpose == TokenPurpose.MANAGE)
    return (
        session.execute(
            select(func.count())
            .select_from(Subscriber)
            .join(Subscription, Subscription.subscriber_id == Subscriber.id)
            .where(
                Subscriber.channel == Channel.WEBPUSH,
                Subscriber.confirmed_at.is_not(None),
                Subscription.id.in_(sent_ok),
                Subscriber.id.not_in(asked),
            )
        ).scalar_one()
        or 0
    )


def run_liveness(
    session: Session, settings: Settings, notifier, now: datetime | None = None
) -> tuple[int, int]:
    """Send the due notifications. Returns (sent, deleted)."""
    from rainalert.tokens import manage_request_token

    now = now or datetime.now(UTC)
    due = due_for_liveness(session, settings, now)
    if not due:
        logger.info("liveness: nothing due")
        return 0, 0

    sent = 0
    gone: list[Subscriber] = []
    for subscriber, subscription in due:
        if not (subscriber.push_p256dh and subscriber.push_auth):
            # Nothing can be encrypted for this row, so it can never be delivered to. Treated as
            # gone rather than left alone: a subscriber who cannot be reached is a location held
            # for nothing, which is the situation this job exists to end.
            logger.warning("liveness: subscriber %s has no push keys - deleting", subscriber.id)
            gone.append(subscriber)
            continue

        token = manage_request_token(
            subscriber.id, settings.secret_key, settings.manage_request_ttl_days
        )
        # Recorded whatever happens, so the run leaves a trace of every subscriber it touched.
        # `event_id` is NULL: this is not about a rain event, which is why the column is nullable.
        #
        # It does *not* make the run crash-safe, and used to claim it did. The row is flushed rather
        # than committed, so a crash rolls it back; and the suppression `due_for_liveness` applies
        # is on `sent_at`, which a queued or expired row does not have. A crashed or failed run
        # therefore re-notifies next month, which is the correct outcome anyway - a month is the
        # retry interval, a week now - but it is not what the old comment said.
        row = Notification(
            subscription_id=subscription.id,
            event_id=None,
            channel=Channel.WEBPUSH.value,
            status="queued",
            queued_at=now,
            payload={"kind": "liveness", "days": settings.webpush_liveness_days},
        )
        session.add(row)
        session.flush()

        try:
            result = notifier.send(liveness_message(settings, subscriber, token))
        except Exception as exc:  # one bad subscriber must not stop the whole run
            row.status = "expired"  # see below: never leave a liveness row queued
            row.error = f"{type(exc).__name__}: {exc}"
            logger.exception("liveness: send raised for subscriber %s", subscriber.id)
            continue

        if result.ok:
            row.status = "sent"
            row.sent_at = now
            # Truncated, like `dispatcher.deliver_queued` does it. This is a push service's
            # `Location` header going into a `String(256)` - the one value in this row whose length
            # a third party controls - and the consequence here is worse than in the dispatcher,
            # because `run_liveness` commits once at the end: an over-long header from a single
            # endpoint raises `DataError` out of that commit and rolls back the *whole run*. Every
            # `sent_at` goes, so those subscribers are due again next week and get a second ping the
            # message itself promises they will not; and every `session.delete(subscriber)` for a
            # subscription the push service just reported 410 on goes with it, so the retention
            # mechanism D-46 exists for quietly stops working - and keeps failing every week for as
            # long as that one endpoint is in the due set.
            row.provider_message_id = (
                result.provider_message_id or None
            ) and result.provider_message_id[:256]
            sent += 1
        else:
            # Never left `queued`. A queued row means "deliver_queued should send this", and that
            # function only knows how to render rain warnings - a liveness row there was a KeyError
            # that took the whole delivery run down with it. There is also nothing to retry: the
            # next weekly run will find this subscriber due again, because `due_for_liveness`
            # measures from `sent_at`, which this row does not have.
            row.status = "expired"
            row.error = result.error
            if result.gone:
                gone.append(subscriber)

    for subscriber in gone:
        logger.info("liveness: deleting subscriber %s - subscription is gone", subscriber.id)
        session.delete(subscriber)
    session.commit()
    logger.info(
        "liveness: %d confirmed push subscriber(s) have never opened their settings",
        count_silent_subscribers(session),
    )
    logger.info("liveness: %d notification(s) sent, %d subscriber(s) deleted", sent, len(gone))
    return sent, len(gone)

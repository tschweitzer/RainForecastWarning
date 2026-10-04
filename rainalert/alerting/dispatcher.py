"""Evaluate every subscription against one cycle, queue mail, then deliver it.

Two structural rules here, both from things that have gone wrong in similar systems:

* **The loop is fault-isolating.** One unevaluatable subscription must not abort the run for
  everyone. Without this, a single bad row is a permanent outage for every subscriber, every cycle
  (SECURITY_REVIEW.md F-3).
* **Queue, then deliver.** Notifications are written in the same transaction as the state change,
  so a crash between the two cannot lose a warning or send one twice. Delivery is a separate pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rainalert.alerting.rules import AlertRule, evaluate
from rainalert.alerting.sampler import SLOTS, MaskCache, sample
from rainalert.alerting.state_machine import Policy, StateView, advance, suppress
from rainalert.config import Settings
from rainalert.db.models import (
    AlertState,
    Evaluation,
    Notification,
    RadarCycle,
    RainEvent,
    Subscriber,
    Subscription,
    SubscriptionAlertState,
    SubscriptionStatus,
)
from rainalert.radar.decoder import RVFrame

logger = logging.getLogger(__name__)

#: A warning older than this is not worth sending. Late rain warnings are worse than none.
MAX_NOTIFICATION_AGE = timedelta(minutes=30)


@dataclass
class CycleReport:
    evaluated: int = 0
    alerts: int = 0
    skipped_missing: int = 0
    errors: int = 0
    blast_radius_tripped: bool = False
    daily_cap_tripped: bool = False
    decisions: dict[str, int] = field(default_factory=dict)


def evaluate_cycle(
    session: Session,
    cycle: RadarCycle,
    frames: list[RVFrame],
    settings: Settings,
    now: datetime | None = None,
) -> CycleReport:
    now = now or datetime.now(UTC)
    report = CycleReport()
    cache = MaskCache()

    subscriptions = (
        session.execute(
            select(Subscription).where(
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.UNHEALTHY])
            )
        )
        .scalars()
        .all()
    )

    pending: list[tuple[Subscription, object, object, object]] = []
    for subscription in subscriptions:
        try:
            outcome = _evaluate_one(session, subscription, cycle, frames, cache, now, settings)
        except Exception:
            # One bad row must not take the cycle down for everyone. The subscription is marked so
            # the user is told, rather than silently never warned.
            report.errors += 1
            logger.exception("evaluation failed for subscription %s", subscription.id)
            subscription.status = SubscriptionStatus.UNHEALTHY
            subscription.health_note = "Dein Standort konnte nicht ausgewertet werden."
            session.commit()
            continue
        if outcome is None:
            continue
        pending.append(outcome)
        report.evaluated += 1

    alerting = [p for p in pending if p[3].alert]
    limit = min(max(1, len(subscriptions) // 2), settings.blast_radius_max)
    if len(alerting) > limit:
        # A cycle that reads as rain everywhere is far more likely to be a broken cycle than a
        # nationwide squall, and mailing the whole list also burns the day's sending quota so the
        # genuine alerts later are never delivered. Queue nothing; page a human.
        report.blast_radius_tripped = True
        logger.error(
            "blast radius tripped: %d of %d subscriptions would be warned in one cycle; "
            "queueing nothing",
            len(alerting),
            len(subscriptions),
        )
        cycle.notes = f"blast radius: {len(alerting)}/{len(subscriptions)} would alert"
        session.commit()

    # The global ceiling (F-2, F-15). Counted once before the loop and then carried forward, so the
    # alerts this cycle is about to queue count against it too - re-querying per subscription would
    # not see them, since nothing is committed until the end.
    day_ago = now - timedelta(hours=24)
    queued_today = alerts_since(session, day_ago) if settings.global_alert_cap_per_day else 0

    for subscription, state_row, reading, transition in pending:
        alert = transition.alert and not report.blast_radius_tripped
        if alert and settings.global_alert_cap_per_day:
            if queued_today >= settings.global_alert_cap_per_day:
                # Shared fate, and that is why it is an error rather than a counter. Unlike the
                # per-subscription cap - where the account being capped is the one that caused it -
                # reaching this means somebody is not warned about weather that is happening, for a
                # reason that is not theirs. If this ever fires, the right response is to find out
                # whether it is real traffic or a bug, not to raise the number.
                alert = False
                transition = replace(transition, alert=False, decision="suppressed_daily_cap")
                if not report.daily_cap_tripped:
                    report.daily_cap_tripped = True
                    logger.error(
                        "daily alert cap reached: %d alerts in 24 h at the %d ceiling; "
                        "further warnings are being suppressed for everyone",
                        queued_today,
                        settings.global_alert_cap_per_day,
                    )
            else:
                queued_today += 1
        _persist(session, subscription, state_row, cycle, reading, transition, alert, now)
        report.decisions[transition.decision] = report.decisions.get(transition.decision, 0) + 1
        if transition.decision == "skipped_missing":
            report.skipped_missing += 1
        report.alerts += alert
    session.commit()
    logger.info(
        "cycle %s evaluated: %d subscriptions, %d alerts, %d skipped, %d errors, %d masks",
        cycle.nominal_time,
        report.evaluated,
        report.alerts,
        report.skipped_missing,
        report.errors,
        len(cache),
    )
    return report


def alerts_since(session, since: datetime, subscription_id=None) -> int:
    """How many *alerts* have been queued since `since`, for one subscription or for all of them.

    `event_id IS NOT NULL` is what makes it alerts rather than notifications: the liveness ping
    writes a `Notification` with `event_id=None` (`jobs/liveness.py`), and counting a six-monthly
    "are you still there" against a subscriber's rain-warning cap would be absurd - and would do it
    silently, by making the cap one alert tighter than it says.

    Counted from `notifications` rather than from `evaluations` because the cap is about what was
    *sent*, not about what was decided. An evaluation that decided to alert and was then suppressed
    for quiet hours cost nobody any quota or reputation.
    """
    stmt = (
        select(func.count())
        .select_from(Notification)
        .where(Notification.event_id.is_not(None), Notification.queued_at >= since)
    )
    if subscription_id is not None:
        stmt = stmt.where(Notification.subscription_id == subscription_id)
    return session.execute(stmt).scalar_one()


def _evaluate_one(session, subscription, cycle, frames, cache, now, settings):
    series = sample(frames, subscription.lat, subscription.lon, subscription.radius_m, cache)
    rule = AlertRule(
        threshold_mm_5min=float(subscription.threshold_mm_5min),
        lead_time_minutes=subscription.lead_time_minutes,
    )
    reading = evaluate(series, rule)

    state_row = session.get(SubscriptionAlertState, subscription.id)
    if state_row is None:
        state_row = SubscriptionAlertState(
            subscription_id=subscription.id,
            state=AlertState.UNKNOWN,
            state_since=now,
            location_applied_at=subscription.location_updated_at,
        )
        session.add(state_row)
        session.flush()

    # A significant move invalidates the state: warning about rain you have just driven into is
    # worse than not warning at all (D-17).
    if subscription.location_updated_at > state_row.location_applied_at:
        state_row.state = AlertState.UNKNOWN
        state_row.state_since = now
        state_row.location_applied_at = subscription.location_updated_at
        state_row.no_hit_cycles = 0

    policy = Policy(
        dry_clear_minutes=settings_dry_clear(subscription),
        min_gap_minutes=subscription.min_gap_minutes,
        alert_cap_per_day=settings.alert_cap_per_subscription_per_day,
        quiet_hours_start=subscription.quiet_hours_start,
        quiet_hours_end=subscription.quiet_hours_end,
        timezone=subscription.timezone,
    )
    view = StateView(
        state_row.state, state_row.dry_since, state_row.no_hit_cycles, state_row.last_alert_at
    )
    transition = suppress(
        advance(view, reading, policy, cycle.nominal_time),
        view,
        policy,
        cycle.nominal_time,
        # Only asked for when the policy has a cap, so the common configuration adds no query.
        alerts_last_day=(
            alerts_since(session, now - timedelta(hours=24), subscription.id)
            if policy.alert_cap_per_day
            else 0
        ),
    )
    return subscription, state_row, (series, reading), transition


def settings_dry_clear(subscription) -> int:
    return 30  # not yet per-subscription; Policy carries it so it can become one


def _persist(session, subscription, state_row, cycle, reading_pair, transition, alert, now):
    series, reading = reading_pair
    session.add(
        Evaluation(
            subscription_id=subscription.id,
            cycle_id=cycle.id,
            evaluated_at=now,
            now_wet=reading.now_wet,
            first_hit_lead_minutes=reading.first_hit_lead_minutes,
            max_rate_by_lead=list(series.max_rate_by_lead),
            missing_fraction=list(series.missing_fraction),
            state_before=state_row.state,
            state_after=transition.next_state,
            decision=transition.decision,
        )
    )
    if state_row.state is not transition.next_state:
        state_row.state_since = now
    state_row.state = transition.next_state
    state_row.dry_since = transition.dry_since
    state_row.no_hit_cycles = transition.no_hit_cycles

    if not alert:
        return

    predicted = cycle.nominal_time + timedelta(minutes=reading.first_hit_lead_minutes or 0)
    event = RainEvent(
        subscription_id=subscription.id, predicted_start_at=predicted, first_alert_at=now
    )
    session.add(event)
    session.flush()
    state_row.current_event_id = event.id
    state_row.last_alert_at = now

    session.add(
        Notification(
            subscription_id=subscription.id,
            event_id=event.id,
            # Set explicitly. The column defaults to "email", so every warning queued for a push
            # subscriber was recorded as a mail - `liveness.py` sets it and this did not. Nothing
            # reads the column yet, which is exactly why it was wrong for as long as it was.
            channel=subscription.subscriber.channel.value,
            status="queued",
            queued_at=now,
            payload={
                "predicted_start_at": predicted.isoformat(),
                # The cycle this was decided from, so the mail can state how fresh the data is
                # rather than when the mail happened to be rendered.
                "cycle_time": cycle.nominal_time.isoformat(),
                "lead_minutes": reading.first_hit_lead_minutes,
                "peak_mm_5min": max(
                    (r for r in series.max_rate_by_lead[:SLOTS] if r is not None), default=0.0
                ),
                "timezone": subscription.timezone,
            },
        )
    )


def deliver_queued(
    session: Session, settings: Settings, notifier, now: datetime | None = None
) -> tuple[int, int]:
    """Send everything queued. Returns (sent, expired).

    Also the main place dead push subscriptions are noticed. A push service answers 404 or 410 once
    a subscription has been revoked - the reader blocked notifications, cleared their site data, or
    uninstalled the browser - and none of those reach us any other way. `DeliveryResult.gone` is
    that signal, and acting on it here is what keeps us from posting to a dead endpoint every time
    it rains, and from holding somebody's coordinates after they have gone (D-46).
    """
    from rainalert.api.mail import alert_message

    now = now or datetime.now(UTC)
    rows = (
        session.execute(select(Notification).where(Notification.status == "queued")).scalars().all()
    )
    # Not every queued row is a rain warning. The liveness job (D-46) writes rows to the same table
    # so that its own sends are recorded and counted, and one of those left `queued` by a transient
    # push failure used to be picked up here and passed to `alert_message`, which reads
    # `payload["predicted_start_at"]` - a KeyError that escaped the per-row try below, aborted the
    # whole run, and rolled back the `sent` status of every warning already delivered in it. So:
    # every five minutes, no warnings at all, and duplicate sends of the ones that had worked.
    #
    # Expired rather than skipped, which is what the filter used to do. A row this function cannot
    # render is a row it will never render, so skipping left it `queued` forever - in the table and
    # in the `notifications_pending` index - and every future run walked past it again. `run_liveness`
    # no longer leaves one queued, so nothing reaches this today; it is here so that the next message
    # kind added to this table fails visibly instead of accumulating.
    sent = expired = 0
    renderable, unknown = [], []
    for row in rows:
        (renderable if "predicted_start_at" in (row.payload or {}) else unknown).append(row)
    rows = renderable
    for row in unknown:
        logger.warning("notification %s is queued but is not a rain warning - expiring it", row.id)
        row.status = "expired"
        row.error = "not a rain warning: deliver_queued cannot render this payload"
        # Counted, so the run reports it. Without this a run that expired only these returned
        # (0, 0) and `jobs/ingest.py`'s `if sent or expired:` logged nothing at all - and the
        # counter is what an operator actually watches.
        expired += 1
    # Collected rather than deleted inside the loop: deleting a subscriber cascades to the very
    # notification rows being iterated, and mutating a collection while walking it is how this
    # would become an intermittent bug instead of an obvious one.
    gone: set = set()
    for row in rows:
        if now - row.queued_at > MAX_NOTIFICATION_AGE:
            row.status = "expired"
            row.error = "older than the useful lifetime of a rain warning"
            expired += 1
            continue
        subscription = session.get(Subscription, row.subscription_id)
        subscriber = session.get(Subscriber, subscription.subscriber_id) if subscription else None
        if subscriber is None:
            row.status = "expired"
            row.error = "subscriber gone"
            expired += 1
            continue
        try:
            # Inside the try, not before it. Building the message reads the payload and the
            # subscriber, and either can be wrong in a way one row does not get to impose on every
            # other row in the batch - which is what happened when it sat outside.
            message = alert_message(session, settings, subscriber, subscription, row.payload)
            result = notifier.send(message)
        except Exception as exc:
            row.error = f"{type(exc).__name__}: {exc}"
            logger.exception("delivery raised for notification %s", row.id)
            continue
        if result.ok:
            row.status = "sent"
            row.sent_at = now
            # Truncated like `error` below it. This is a `Location` header from a push service -
            # the one value written here that a third party controls the length of - going into a
            # `String(256)`. Observed lengths are 60-110, so this is a guard rather than a fix, but
            # an over-long one would be a `DataError` on the commit that rolls back the whole batch,
            # including the rows that were delivered successfully.
            row.provider_message_id = (
                result.provider_message_id or None
            ) and result.provider_message_id[:256]
            sent += 1
        else:
            row.error = result.error
            if result.gone:
                # Not an error to retry: this address will never accept anything again.
                row.status = "expired"
                expired += 1
                gone.add(subscriber.id)

    for subscriber_id in gone:
        doomed = session.get(Subscriber, subscriber_id)
        if doomed is not None:
            logger.info(
                "deleting subscriber %s: the push service says the subscription is gone",
                subscriber_id,
            )
            session.delete(doomed)
    session.commit()
    return sent, expired


def purge_evaluations(session: Session, settings: Settings, now: datetime | None = None) -> int:
    """The 48 h debug window (D-23). The durable record is rain_events + notifications."""
    from sqlalchemy import delete

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=settings.evaluation_retention_hours)
    result = session.execute(delete(Evaluation).where(Evaluation.evaluated_at < cutoff))
    session.commit()
    return result.rowcount or 0

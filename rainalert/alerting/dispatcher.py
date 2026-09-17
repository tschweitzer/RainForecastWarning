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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
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
            outcome = _evaluate_one(session, subscription, cycle, frames, cache, now)
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

    for subscription, state_row, reading, transition in pending:
        alert = transition.alert and not report.blast_radius_tripped
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


def _evaluate_one(session, subscription, cycle, frames, cache, now):
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
        quiet_hours_start=subscription.quiet_hours_start,
        quiet_hours_end=subscription.quiet_hours_end,
        timezone=subscription.timezone,
    )
    view = StateView(
        state_row.state, state_row.dry_since, state_row.no_hit_cycles, state_row.last_alert_at
    )
    transition = suppress(
        advance(view, reading, policy, cycle.nominal_time), view, policy, cycle.nominal_time
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
    """Send everything queued. Returns (sent, expired)."""
    from rainalert.api.mail import alert_message

    now = now or datetime.now(UTC)
    rows = (
        session.execute(select(Notification).where(Notification.status == "queued")).scalars().all()
    )
    sent = expired = 0
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
        message = alert_message(session, settings, subscriber, subscription, row.payload)
        try:
            result = notifier.send(message)
        except Exception as exc:
            row.error = f"{type(exc).__name__}: {exc}"
            logger.exception("delivery raised for notification %s", row.id)
            continue
        if result.ok:
            row.status = "sent"
            row.sent_at = now
            row.provider_message_id = result.provider_message_id
            sent += 1
        else:
            row.error = result.error
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

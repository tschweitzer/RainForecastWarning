"""The whole alerting path against a real database: evaluate, queue, deliver.

The scenario is a rain front that is not yet overhead but will be: the case the service exists for.
"""

from datetime import UTC, datetime, timedelta

import pytest

from rainalert.alerting.dispatcher import (
    deliver_queued,
    evaluate_cycle,
    purge_evaluations,
)
from rainalert.config import Settings
from rainalert.db.models import (
    AlertState,
    CycleStatus,
    Evaluation,
    Notification,
    RadarCycle,
    RainEvent,
    SubscriptionAlertState,
    SubscriptionStatus,
)
from rainalert.notify import ConsoleNotifier
from rainalert.radar.decoder import read_frames
from rainalert.radar.grid import cell_of

T0 = datetime(2026, 9, 16, 13, 55, tzinfo=UTC)
FRANKFURT = (50.1109, 8.6821)


@pytest.fixture()
def settings():
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.org",
        secret_key="test-secret",
        _env_file=None,
    )


@pytest.fixture()
def frames(wet_cycle):
    """A real cycle, with rain painted in at the leads we want."""
    return read_frames(wet_cycle)


def make_frames(base_frames, *, wet_now: bool, wet_leads: tuple[int, ...], at=FRANKFURT):
    """Copy real frames and set the cells around a point, so coverage/masking stay realistic."""
    row, col = cell_of(*at)
    out = []
    for frame in base_frames:
        values = frame.values.copy()
        values[row - 3 : row + 4, col - 3 : col + 4] = 0.0
        wet = (frame.lead_minutes == 0 and wet_now) or (frame.lead_minutes in wet_leads)
        if wet:
            values[row - 1 : row + 2, col - 1 : col + 2] = 1.5
        out.append(
            type(frame)(
                values=values,
                missing=frame.missing.copy(),
                nominal_time=frame.nominal_time,
                lead_minutes=frame.lead_minutes,
                interval_minutes=frame.interval_minutes,
                precision=frame.precision,
                radar_sites=frame.radar_sites,
                raw_header=frame.raw_header,
            )
        )
    return out


def warm_up(session, settings, frames, at_time=None):
    """Run one dry cycle so the subscription leaves UNKNOWN.

    Per the §9 table a fresh subscription observes before it warns: its first cycle establishes
    that it is currently dry. Real subscribers cross this within five minutes of confirming.
    """
    at_time = at_time or (T0 - timedelta(minutes=5))
    dry = make_frames(frames, wet_now=False, wet_leads=())
    evaluate_cycle(session, add_cycle(session, at_time), dry, settings, now=at_time)


def add_cycle(session, nominal=T0):
    cycle = RadarCycle(
        nominal_time=nominal,
        fetched_at=nominal,
        source_url="test",
        sha256=b"\x00" * 32,
        bytes=1,
        frame_count=3,
        status=CycleStatus.OK,
    )
    session.add(cycle)
    session.commit()
    return cycle


def active_subscription(session, settings, email="friend@example.com", at=FRANKFURT):
    from rainalert import subscriptions as svc
    from rainalert.db.models import Subscription

    result = svc.subscribe(session, settings, address=email, lat=at[0], lon=at[1])
    confirmed = svc.confirm(session, settings, token=result.confirm_token)
    return (
        session.query(Subscription)
        .filter(Subscription.subscriber_id == confirmed.subscriber_id)
        .one()
    )


# --- the happy path --------------------------------------------------------------------------


def test_rain_coming_produces_exactly_one_warning(db, settings, frames):
    notifier = ConsoleNotifier()
    with db() as session:
        sub = active_subscription(session, settings)
        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        # frame set has leads 0/60/120; a hit at +60 needs a lead window that reaches it
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)

        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        assert report.alerts == 1
        assert session.query(RainEvent).count() == 1
        assert session.query(Notification).one().status == "queued"
        assert session.get(SubscriptionAlertState, sub.id).state is AlertState.WARNED

        sent, expired = deliver_queued(session, settings, notifier, now=T0)
        assert (sent, expired) == (1, 0)
        assert session.query(Notification).one().status == "sent"

    message = notifier.sent[0]
    assert "Regen in etwa 60 Minuten" == message.subject
    # The fragment form, so the token cannot reach a log (D-26) - in the body and in the header
    # alike, which is only possible because the header no longer promises it may be POSTed.
    assert "/unsubscribe#t=" in message.text
    assert (
        message.headers["List-Unsubscribe"] == f"<{message.text.split('Abmelden: ')[1].split()[0]}>"
    )
    assert "List-Unsubscribe-Post" not in message.headers, (
        "one-click was advertised and answered 400; do not re-add it without the handler (Q-13)"
    )
    assert "?token=" not in str(message.headers)


def test_a_second_cycle_of_the_same_front_sends_nothing(db, settings, frames):
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        evaluate_cycle(session, add_cycle(session, T0), approaching, settings, now=T0)
        evaluate_cycle(
            session,
            add_cycle(session, T0 + timedelta(minutes=5)),
            approaching,
            settings,
            now=T0 + timedelta(minutes=5),
        )
        assert session.query(Notification).count() == 1


def test_already_raining_does_not_warn(db, settings, frames):
    with db() as session:
        active_subscription(session, settings)
        overhead = make_frames(frames, wet_now=True, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), overhead, settings, now=T0)
        assert report.alerts == 0
        assert session.query(Notification).count() == 0


def test_dry_everywhere_warns_nobody(db, settings, frames):
    with db() as session:
        active_subscription(session, settings)
        dry = make_frames(frames, wet_now=False, wet_leads=())
        assert evaluate_cycle(session, add_cycle(session), dry, settings, now=T0).alerts == 0


# --- resilience ------------------------------------------------------------------------------


def test_one_broken_subscription_does_not_stop_the_others(db, settings, frames, monkeypatch):
    """A single unevaluatable row must not be a permanent outage for everyone (F-3).

    The failure is injected rather than contrived from data: what is being tested is the isolation
    itself, so it should hold for *any* cause, not just the one we thought of.
    """
    from rainalert.alerting import dispatcher
    from rainalert.db.models import Subscription

    with db() as session:
        good = active_subscription(session, settings, email="good@example.com")
        good.lead_time_minutes = 60
        bad = active_subscription(session, settings, email="bad@example.com", at=(51.0, 9.0))
        session.commit()
        bad_id, good_id = bad.id, good.id
        warm_up(session, settings, frames)

        real_sample = dispatcher.sample

        def exploding_sample(frames_, lat, lon, radius_m, cache=None):
            if (round(lat, 3), round(lon, 3)) == (51.0, 9.0):
                raise RuntimeError("boom")
            return real_sample(frames_, lat, lon, radius_m, cache)

        monkeypatch.setattr(dispatcher, "sample", exploding_sample)

        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)

        assert report.alerts == 1  # the healthy one still got its warning
        assert report.errors == 1
        session.expire_all()
        assert session.get(Subscription, bad_id).status is SubscriptionStatus.UNHEALTHY
        assert session.get(Subscription, bad_id).health_note
        assert session.get(Subscription, good_id).status is SubscriptionStatus.ACTIVE


def test_blast_radius_limiter_queues_nothing(db, settings, frames):
    """A cycle that would warn everyone is likelier broken than nationwide (F-2)."""
    settings.blast_radius_max = 1
    with db() as session:
        for index in range(4):
            sub = active_subscription(session, settings, email=f"f{index}@example.com")
            sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        assert report.blast_radius_tripped
        assert report.alerts == 0
        assert session.query(Notification).count() == 0
        # ...but the state still advanced, so the event is not re-warned forever
        assert session.query(Evaluation).count() == 8  # 4 warm-up + 4 this cycle


def test_missing_data_is_not_reported_as_dry(db, settings, frames):
    with db() as session:
        sub = active_subscription(session, settings)
        blind = make_frames(frames, wet_now=False, wet_leads=())
        for frame in blind:
            frame.missing[:] = True
        report = evaluate_cycle(session, add_cycle(session), blind, settings, now=T0)
        assert report.skipped_missing == 1
        assert report.alerts == 0
        assert session.get(SubscriptionAlertState, sub.id).state is AlertState.UNKNOWN
        assert session.query(Evaluation).one().decision == "skipped_missing"


def test_stale_notifications_expire_rather_than_send(db, settings, frames):
    notifier = ConsoleNotifier()
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        evaluate_cycle(
            session,
            add_cycle(session),
            make_frames(frames, wet_now=False, wet_leads=(60,)),
            settings,
            now=T0,
        )
        sent, expired = deliver_queued(session, settings, notifier, now=T0 + timedelta(hours=2))
    assert (sent, expired) == (0, 1)
    assert notifier.sent == []


def test_evaluations_are_purged_but_events_are_not(db, settings, frames):
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        evaluate_cycle(
            session,
            add_cycle(session),
            make_frames(frames, wet_now=False, wet_leads=(60,)),
            settings,
            now=T0,
        )
        assert session.query(Evaluation).count() == 2
        removed = purge_evaluations(session, settings, now=T0 + timedelta(hours=72))
        assert removed == 2
        assert session.query(Evaluation).count() == 0
        assert session.query(RainEvent).count() == 1  # the durable record survives
        assert session.query(Notification).count() == 1


# --- verification ----------------------------------------------------------------------------


def test_verification_scores_a_warning_that_came_true(db, settings, frames):
    from rainalert.jobs.verify import verify_events

    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        evaluate_cycle(
            session,
            add_cycle(session, T0),
            make_frames(frames, wet_now=False, wet_leads=(60,)),
            settings,
            now=T0,
        )
        # an hour later the rain is actually overhead
        arrival = T0 + timedelta(minutes=60)
        evaluate_cycle(
            session,
            add_cycle(session, arrival),
            make_frames(frames, wet_now=True, wet_leads=()),
            settings,
            now=arrival,
        )
        report = verify_events(session, now=arrival + timedelta(hours=1))
        assert (report.judged, report.hits, report.misses) == (1, 1, 0)
        assert session.query(RainEvent).one().verified is True


def test_verification_scores_a_false_alarm(db, settings, frames):
    from rainalert.jobs.verify import verify_events

    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        evaluate_cycle(
            session,
            add_cycle(session, T0),
            make_frames(frames, wet_now=False, wet_leads=(60,)),
            settings,
            now=T0,
        )
        # the rain never arrives
        for step in range(1, 20):
            at = T0 + timedelta(minutes=5 * step)
            evaluate_cycle(
                session,
                add_cycle(session, at),
                make_frames(frames, wet_now=False, wet_leads=()),
                settings,
                now=at,
            )
        report = verify_events(session, now=T0 + timedelta(hours=3))
        assert (report.judged, report.hits, report.misses) == (1, 0, 1)
        assert session.query(RainEvent).one().verified is False


def test_verification_does_not_judge_an_event_too_early(db, settings, frames):
    """A warning made minutes ago must not be scored before the rain had a chance to arrive."""
    from rainalert.jobs.verify import verify_events

    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        evaluate_cycle(
            session,
            add_cycle(session, T0),
            make_frames(frames, wet_now=False, wet_leads=(60,)),
            settings,
            now=T0,
        )
        assert verify_events(session, now=T0 + timedelta(minutes=10)).judged == 0
        assert session.query(RainEvent).one().verified is None


def test_the_mail_states_the_data_time_not_the_render_time(db, settings, frames):
    """A warning that misstates how fresh its data is undermines the reader's only way to judge it."""
    notifier = ConsoleNotifier()
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        evaluate_cycle(
            session,
            add_cycle(session, T0),
            make_frames(frames, wet_now=False, wet_leads=(60,)),
            settings,
            now=T0,
        )
        deliver_queued(session, settings, notifier, now=T0 + timedelta(minutes=2))
    body = notifier.sent[0].text
    # T0 is 13:55 UTC = 15:55 Europe/Berlin
    assert "Radarbild von 15:55 Uhr" in body

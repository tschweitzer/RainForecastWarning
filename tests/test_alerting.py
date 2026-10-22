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


def active_subscription(session, settings, name="friend", at=FRANKFURT):
    from rainalert import subscriptions as svc
    from rainalert.db.models import Subscription

    result = svc.subscribe(
        session,
        settings,
        endpoint=f"https://fcm.googleapis.com/fcm/send/{name}",
        push_p256dh="k" * 87,
        push_auth="a" * 22,
        lat=at[0],
        lon=at[1],
    )
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
    # Tapping it opens the map at the warned place, by a signed reference in the fragment (D-26);
    # the body prints no URL, because nothing in a notification shade could open one.
    assert "#l=" in message.click_url and "?token=" not in message.click_url
    assert "http" not in message.text


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
        good = active_subscription(session, settings, name="good")
        good.lead_time_minutes = 60
        bad = active_subscription(session, settings, name="bad", at=(51.0, 9.0))
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


def wet_everywhere(base_frames, leads=(0,)):
    """A corrupt "rain everywhere" cycle: every cell with data wet, at the given leads."""
    out = make_frames(base_frames, wet_now=False, wet_leads=())
    for frame in out:
        if frame.lead_minutes in leads:
            frame.values[~frame.missing] = 1.5
    return out


def test_a_cycle_that_turns_wet_everywhere_is_set_aside(db, settings, frames, caplog):
    """D-70, which replaced the blast radius: judged from the radar data, not the subscriber count.

    Set aside, the cycle changes nothing - no notification, no state moved - and says so at ERROR.
    The next good cycle evaluates as if it had never happened.
    """
    import logging

    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        assert session.get(SubscriptionAlertState, sub.id).state is AlertState.DRY

        cycle = add_cycle(session)
        with caplog.at_level(logging.ERROR):
            report = evaluate_cycle(session, cycle, wet_everywhere(frames), settings, now=T0)

        assert report.implausible and "rose from" in report.implausible
        assert "set aside as implausible" in caplog.text
        assert report.alerts == 0
        assert report.decisions == {"skipped_implausible": 1}
        assert session.query(Notification).count() == 0
        assert session.get(SubscriptionAlertState, sub.id).state is AlertState.DRY
        assert cycle.notes.startswith("implausible: ")
        # Not remembered, or a second broken cycle would compare against the first and pass.
        assert cycle.wet_share is None

        # Five minutes later, real rain on the way: compared with the warm-up, not the broken one.
        later = T0 + timedelta(minutes=5)
        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(
            session, add_cycle(session, later), approaching, settings, now=later
        )
        assert report.implausible is None
        assert report.alerts == 1


def test_two_broken_cycles_in_a_row_are_both_set_aside(db, settings, frames):
    with db() as session:
        active_subscription(session, settings)
        warm_up(session, settings, frames)
        for minutes in (0, 5):
            at = T0 + timedelta(minutes=minutes)
            report = evaluate_cycle(
                session, add_cycle(session, at), wet_everywhere(frames), settings, now=at
            )
            assert report.implausible, f"the cycle {minutes} minutes in was believed"


def test_a_corrupt_forecast_frame_is_set_aside_too(db, settings, frames):
    """The frame that would do the warning. Caught even with no recent cycle to compare t+0 to."""
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        report = evaluate_cycle(
            session, add_cycle(session), wet_everywhere(frames, leads=(60,)), settings, now=T0
        )
        assert report.implausible and "forecast" in report.implausible
        assert session.get(SubscriptionAlertState, sub.id).state is AlertState.UNKNOWN


def test_everyone_warned_at_once_is_not_a_reason_to_warn_nobody(db, settings, frames):
    """What the blast radius got wrong: five subscribers in one town, one shower, all of them warned
    at once - ordinary weather that it took for a broken cycle, and dropped for good."""
    with db() as session:
        for index in range(5):
            sub = active_subscription(session, settings, name=f"f{index}")
            sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        assert report.implausible is None
        assert report.alerts == 5
        assert session.query(Notification).count() == 5


def test_the_plausibility_check_can_be_turned_off(db, settings, frames):
    with db() as session:
        active_subscription(session, settings)
        warm_up(session, settings, frames)
        off = settings.model_copy(update={"implausible_rise_pp": 0})
        report = evaluate_cycle(session, add_cycle(session), wet_everywhere(frames), off, now=T0)
        assert report.implausible is None
        assert report.alerts == 1  # rain overhead, unwarned: "Es regnet jetzt"


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


def test_the_ingest_job_keeps_every_retention_promise(db, settings, frames):
    """D-73: verification and both purges ran from nowhere in production. Now every cycle."""
    from rainalert.api.ratelimit import ip_bucket
    from rainalert.db.models import RateLimitHit
    from rainalert.jobs.ingest import housekeeping

    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        session.add(
            RateLimitHit(
                bucket=ip_bucket("subscribe:ip", "203.0.113.7", settings.secret_key),
                occurred_at=T0,
            )
        )
        session.commit()

        # Two hours on: the warning is due for judging, nothing is old enough to purge yet.
        housekeeping(session, settings, T0 + timedelta(hours=2))
        event = session.query(RainEvent).one()
        assert event.verified is not None, "the warning was never scored"
        assert session.query(Evaluation).count() == 2

        # Eight days on: the evaluation log and the counters are past their retention.
        housekeeping(session, settings, T0 + timedelta(days=8))
        assert session.query(Evaluation).count() == 0
        assert session.query(RateLimitHit).count() == 0
        assert session.query(RainEvent).count() == 1, "the durable record stays"


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


def test_the_warning_states_the_data_time_not_the_render_time(db, settings, frames):
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


# --- the caps nobody can turn off (SECURITY_REVIEW.md F-15, F-2) --------------------------------


def _past_alerts(session, subscription, how_many, now, *, as_liveness=False):
    """Pre-load the rolling window with alerts (or liveness pings) already queued."""
    from rainalert.db.models import Notification, RainEvent

    for i in range(how_many):
        event_id = None
        if not as_liveness:
            event = RainEvent(
                subscription_id=subscription.id,
                predicted_start_at=now - timedelta(hours=i + 1),
                first_alert_at=now - timedelta(hours=i + 1),
            )
            session.add(event)
            session.flush()
            event_id = event.id
        session.add(
            Notification(
                subscription_id=subscription.id,
                event_id=event_id,
                status="sent",
                queued_at=now - timedelta(hours=i + 1),
                payload={},
            )
        )
    session.commit()


def test_a_subscription_at_its_daily_cap_is_not_warned_again(db, settings, frames):
    """F-15. `threshold=0.01, lead=120, radius=20000` is within spec on every axis and together
    means "close to always" in a German autumn. What it spends is shared - one VAPID key's standing
    with the push services - so the ceiling is not the subscriber's to set."""
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        _past_alerts(session, sub, settings.alert_cap_per_subscription_per_day, T0)

        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)

        assert report.alerts == 0
        assert report.decisions.get("suppressed_cap") == 1
        # Visible in `evaluations`, which is what F-15 asked for: suppressed silently would be
        # indistinguishable from "it never rained".
        from rainalert.db.models import Evaluation

        latest = session.query(Evaluation).order_by(Evaluation.evaluated_at.desc()).first()
        assert latest.decision == "suppressed_cap"


def test_the_cap_still_advances_the_state(db, settings, frames):
    """Same rule the other two throttles follow: the send is dropped, the state is not rolled back.

    If it rolled back, the identical event would re-fire on the very next cycle and the cap would
    have achieved nothing but a delay.
    """
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        _past_alerts(session, sub, settings.alert_cap_per_subscription_per_day, T0)

        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        assert session.get(SubscriptionAlertState, sub.id).state is AlertState.WARNED


def test_one_under_the_cap_still_warns(db, settings, frames):
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        _past_alerts(session, sub, settings.alert_cap_per_subscription_per_day - 1, T0)

        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        assert report.alerts == 1


def test_alerts_older_than_the_window_do_not_count(db, settings, frames):
    """It is a *rolling* 24 h. Yesterday's weather must not mute today's."""
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        # All of them a comfortable margin outside the window.
        _past_alerts(
            session,
            sub,
            settings.alert_cap_per_subscription_per_day,
            T0 - timedelta(hours=25),
        )

        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        assert report.alerts == 1


def test_liveness_pings_do_not_count_against_the_cap(db, settings, frames):
    """A six-monthly "are you still there" is not a rain warning, and counting it would make the cap
    one tighter than it says - silently, which is the worst way to be wrong about a limit."""
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        _past_alerts(
            session,
            sub,
            settings.alert_cap_per_subscription_per_day + 5,
            T0,
            as_liveness=True,
        )

        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        assert report.alerts == 1


def test_the_cap_can_be_turned_off(db, settings, frames):
    """0 disables it, for a deployment that would rather have the noise than the ceiling."""
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        _past_alerts(session, sub, 50, T0)

        loose = settings.model_copy(update={"alert_cap_per_subscription_per_day": 0})
        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, loose, now=T0)
        assert report.alerts == 1


def _push_subscription(
    session, settings, endpoint="https://fcm.googleapis.com/fcm/send/alerting-test"
):
    from rainalert import subscriptions as svc
    from rainalert.db.models import Subscriber

    result = svc.subscribe(
        session,
        settings,
        endpoint=endpoint,
        push_p256dh="k" * 87,
        push_auth="a" * 22,
        lat=FRANKFURT[0],
        lon=FRANKFURT[1],
    )
    subscriber = session.get(Subscriber, result.subscriber_id)
    subscriber.confirmed_at = T0 - timedelta(days=1)
    subscription = subscriber.subscriptions[0]
    subscription.status = SubscriptionStatus.ACTIVE
    session.commit()
    return subscription


def test_reaching_the_per_subscription_ceiling_is_logged(db, settings, frames, caplog):
    """24 a day is out of reach of real weather (D-70), so a hit is worth seeing."""
    import logging

    assert settings.alert_cap_per_subscription_per_day == 24
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        _past_alerts(session, sub, 24, T0)

        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        with caplog.at_level(logging.WARNING):
            report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)
        assert report.alerts == 0
        assert "reached its ceiling of 24 rain notifications" in caplog.text


# --- delivery: lifetime and concurrency (D-70) -----------------------------------------------


def test_a_warning_is_delivered_for_as_long_as_it_is_true(db, settings, frames):
    """Rain 60 minutes out, phone offline for 40: the 30-minute rule dropped this warning, though
    the rain had not even started. And the push service is told to hold it only until it has."""
    notifier = ConsoleNotifier()
    with db() as session:
        sub = active_subscription(session, settings)
        sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)

        sent, expired = deliver_queued(session, settings, notifier, now=T0 + timedelta(minutes=40))
    assert (sent, expired) == (1, 0)
    assert notifier.sent[0].ttl_seconds == 20 * 60


def test_es_regnet_jetzt_expires_after_a_quarter_of_an_hour(db, settings, frames):
    notifier = ConsoleNotifier()
    with db() as session:
        active_subscription(session, settings)
        warm_up(session, settings, frames)
        raining = make_frames(frames, wet_now=True, wet_leads=(0, 5, 10, 15))
        report = evaluate_cycle(session, add_cycle(session), raining, settings, now=T0)
        assert report.alerts == 1

        assert deliver_queued(session, settings, notifier, now=T0 + timedelta(minutes=16)) == (
            0,
            1,
        )
    assert notifier.sent == []


def test_push_is_sent_concurrently_and_one_failure_stays_its_own():
    """Order kept, and an exception confined to its message."""
    import threading

    from rainalert.alerting.dispatcher import _send_all
    from rainalert.notify.base import DeliveryResult, OutboundMessage

    settings = Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.org",
        secret_key="test-secret",
        delivery_concurrency=4,
        _env_file=None,
    )
    barrier = threading.Barrier(4, timeout=5)
    seen = []

    class Recording:
        def send(self, message):
            # Four pushes meet here at once, or this times out: proof they run side by side.
            barrier.wait()
            seen.append(threading.current_thread().name)
            if message.to.endswith("/2"):
                raise RuntimeError("push service exploded")
            return DeliveryResult(ok=True, provider_message_id=message.to)

    def push(n):
        return OutboundMessage(
            to=f"https://push.example.invalid/{n}",
            subject="s",
            text="t",
            push_p256dh="k" * 87,
            push_auth="a" * 22,
        )

    messages = [push(0), push(1), push(2), push(3)]
    outcomes = _send_all(Recording(), messages, settings)

    assert [getattr(o, "provider_message_id", None) for o in outcomes] == [
        "https://push.example.invalid/0",
        "https://push.example.invalid/1",
        None,
        "https://push.example.invalid/3",
    ]
    assert isinstance(outcomes[2], RuntimeError)
    assert all(name.startswith("push") for name in seen)


# --- one unreadable subscriber must not cost everyone their warning (F-3, 2026-10-05) -----------


def test_an_unloadable_subscriber_does_not_take_the_cycle_down(db, settings, frames):
    """Reproduces the production crash of 2026-10-05.

    An `NTFY` subscriber survived its deletion migration (f3b8c21e7a94 deleted lowercase `ntfy`;
    the ORM stores the name `NTFY`). Once `Channel.NTFY` left the enum the row could not be loaded,
    and `_persist` lazy-loads the subscriber on the alert path - so when rain approached that
    location the whole cycle raised. Every *other* subscriber's warning went with it, and since the
    cycle row is committed before evaluation, none of them was ever retried.

    F-3's isolation covered evaluation and not persisting. This asserts the half that was missing:
    the healthy subscriber is warned anyway, and what it was warned with is actually committed.

    The channel column that failure came through is no longer mapped (D-76), so the failure is now
    made by a trigger: the broken subscriber's notification insert raises, after the RainEvent has
    been flushed - the same place, inside `_persist`, that the lazy load used to raise.
    """
    from sqlalchemy import text

    from rainalert.db.models import Notification, Subscription

    with db() as session:
        healthy = active_subscription(session, settings, name="healthy")
        broken = active_subscription(session, settings, name="broken")
        # Two more, dry and far away. Kept from when a cycle warning 2 of 2 subscribers tripped the
        # blast radius (gone since D-70): real rain is local, and this makes it look local.
        for i, place in enumerate(((53.5511, 9.9937), (48.1351, 11.5820))):
            active_subscription(session, settings, name=f"dry{i}", at=place)
        for sub in (healthy, broken):
            sub.lead_time_minutes = 60
        session.commit()
        warm_up(session, settings, frames)
        healthy_id, broken_id = healthy.id, broken.id

        session.execute(
            text(
                "CREATE FUNCTION refuse_insert() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN RAISE EXCEPTION 'unloadable subscriber'; END $$"
            )
        )
        session.execute(
            text(
                "CREATE TRIGGER broken_subscriber BEFORE INSERT ON notifications FOR EACH ROW "
                f"WHEN (NEW.subscription_id = '{broken.id}') EXECUTE FUNCTION refuse_insert()"
            )
        )
        session.commit()
        # So the lazy load really goes back to the database, as it did in the job's fresh process.
        session.expire_all()

        approaching = make_frames(frames, wet_now=False, wet_leads=(60,))
        report = evaluate_cycle(session, add_cycle(session), approaching, settings, now=T0)

        assert report.errors == 1
        assert report.alerts == 1, "the healthy subscriber must still be warned"

    # A fresh session, because "warned" has to mean committed, not merely pending in this one.
    with db() as session:
        queued = session.query(Notification).filter_by(subscription_id=healthy_id).all()
        assert len(queued) == 1, "the healthy subscriber's warning was not persisted"
        assert session.get(Subscription, broken_id).status is SubscriptionStatus.UNHEALTHY
        # The half-write a bare `try` would leave. `_persist` adds a RainEvent and flushes it
        # *before* the line that raised, so without the SAVEPOINT the broken subscriber would be
        # committed with an event nobody was ever told about and its state moved to WARNED.
        assert not session.query(RainEvent).filter_by(subscription_id=broken_id).all(), (
            "the broken subscriber was committed half-written - that is what the SAVEPOINT is for"
        )


# --- "Es regnet jetzt" (D-69) ----------------------------------------------------------------


def test_rain_that_starts_without_a_warning_says_es_regnet_jetzt(db, settings, frames):
    """Drizzle forming overhead, a shower faster than the nowcast: it used to pass in silence."""
    notifier = ConsoleNotifier()
    with db() as session:
        sub = active_subscription(session, settings)
        warm_up(session, settings, frames)  # now DRY
        overhead = make_frames(frames, wet_now=True, wet_leads=())
        report = evaluate_cycle(session, add_cycle(session), overhead, settings, now=T0)
        assert report.alerts == 1
        assert report.decisions.get("rain_started_unwarned") == 1
        assert session.get(SubscriptionAlertState, sub.id).state is AlertState.RAINING
        note = session.query(Notification).one()
        assert note.payload["kind"] == "rain_now"
        assert note.payload["lead_minutes"] == 0
        # Born with its observed start: there was no forecast to judge.
        event = session.query(RainEvent).one()
        assert event.observed_start_at == T0 and event.verified is None

        assert deliver_queued(session, settings, notifier, now=T0) == (1, 0)

    message = notifier.sent[0]
    assert message.subject == "Es regnet jetzt"
    assert "eingesetzt" in message.text and "nicht angekündigt" in message.text
    # Same map link as a warning.
    assert "#l=" in message.click_url


def test_rain_now_is_said_once_per_rain_event(db, settings, frames):
    with db() as session:
        active_subscription(session, settings)
        warm_up(session, settings, frames)
        overhead = make_frames(frames, wet_now=True, wet_leads=())
        dry = make_frames(frames, wet_now=False, wet_leads=())
        timeline = [overhead, overhead, dry, overhead]  # a ten-minute break is the same event
        for i, cycle_frames in enumerate(timeline):
            at = T0 + timedelta(minutes=5 * i)
            evaluate_cycle(session, add_cycle(session, at), cycle_frames, settings, now=at)
        assert session.query(Notification).count() == 1


def test_rain_that_was_warned_about_is_not_announced_again(db, settings, frames):
    """WARNED -> RAINING stays silent: the reader was told already."""
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
        later = T0 + timedelta(minutes=5)
        evaluate_cycle(
            session,
            add_cycle(session, later),
            make_frames(frames, wet_now=True, wet_leads=()),
            settings,
            now=later,
        )
        kinds = [n.payload["kind"] for n in session.query(Notification).all()]
        assert kinds == ["warning"]


def test_rain_now_counts_against_the_daily_cap(db, settings, frames):
    settings.alert_cap_per_subscription_per_day = 2
    with db() as session:
        sub = active_subscription(session, settings)
        warm_up(session, settings, frames)
        _past_alerts(session, sub, 2, T0)
        overhead = make_frames(frames, wet_now=True, wet_leads=())
        report = evaluate_cycle(session, add_cycle(session), overhead, settings, now=T0)
        assert report.alerts == 0
        assert report.decisions.get("suppressed_cap") == 1
        assert session.get(SubscriptionAlertState, sub.id).state is AlertState.RAINING


def test_verification_does_not_score_rain_that_was_never_forecast(db, settings, frames):
    """Scoring it would count, as a forecast hit, exactly the rain the forecast missed."""
    from rainalert.jobs.verify import verify_events

    with db() as session:
        active_subscription(session, settings)
        warm_up(session, settings, frames)
        overhead = make_frames(frames, wet_now=True, wet_leads=())
        evaluate_cycle(session, add_cycle(session), overhead, settings, now=T0)
        report = verify_events(session, now=T0 + timedelta(hours=2))
        assert report.judged == 0
        assert session.query(RainEvent).one().verified is None

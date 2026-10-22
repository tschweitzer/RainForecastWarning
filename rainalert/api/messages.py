"""The messages we send - all of them web push notifications (D-76).

A notification shows a line collapsed and three expanded, its body is plain text that no platform
linkifies, and tapping it opens `click_url`. Every builder here is shaped by those three facts: one
thing to say, no URLs in the body, and the link where a tap goes.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from rainalert.config import Settings
from rainalert.notify import OutboundMessage
from rainalert.tokens import locate_token

#: Which notifications replace each other in the shade. Two families, because one tag for
#: everything meant a settings link - or the service worker's acknowledgement of the tap that asked
#: for one - replaced a live rain warning and its map link at the moment the reader wanted it.
#:
#: Within a family, replacing is the point: a shower that keeps re-triggering must not leave a column
#: of near-identical warnings, and a settings link should supersede the one before it.
#:
#: `MANAGE_TAG` is duplicated as `MANAGE_TAG` in `static/sw.js`, which uses it for the
#: acknowledgement it shows itself; `tests/test_webpush.py` asserts the two agree.
ALERT_TAG = "rainalert-alert"
MANAGE_TAG = "rainalert-manage"


def push_keys(subscriber) -> dict:
    """The RFC 8291 keys as kwargs. `OutboundMessage` requires them, so leaving this out of a
    builder fails at construction - which is how the omission that once made the whole channel
    undeliverable was found."""
    return {"push_p256dh": subscriber.push_p256dh, "push_auth": subscriber.push_auth}


def confirmation_message(settings: Settings, subscriber, token: str) -> OutboundMessage:
    """The double opt-in message.

    `#a=` means "confirm on open". SECURITY_REVIEW.md F-4 - a link scanner spending the token - is
    what a second click on the page would defend against, and every actor it names is a *mail*
    scanner. None of them sits between this service and a notification on someone's phone, so the
    page confirms as soon as it opens.
    """
    # Does not say "ohne Bestätigung wird nichts gespeichert": it was not true. The pending row
    # exists from the moment of signup, carrying the coordinates and a salted IP hash, which is what
    # `privacy.html` says. What is true is that an unconfirmed signup is deleted after
    # `unconfirmed_purge_hours` - enforced by `purge_unconfirmed` in the ingest job.
    link = f"{settings.public_base_url.rstrip('/')}/confirm#a={token}"
    # Action first: Android shows one line collapsed. No URL in the body - tapping is the way
    # forward, and that is what `click_url` is - and no licence footer under the most important
    # notification in the flow; the attribution is on every page of the site.
    text = f"""Zum Aktivieren antippen. Dann weißt du, dass die Warnungen bei dir ankommen.

Gültig {settings.confirm_token_ttl_hours} Stunden, einmal benutzbar. Ohne Bestätigung löschen wir
die Anmeldung nach {settings.unconfirmed_purge_hours} Stunden und es kommt nichts weiter.
"""
    return OutboundMessage(
        to=subscriber.address,
        **push_keys(subscriber),
        subject="Regenwarnung bestätigen",
        text=text,
        click_url=link,
        # As long as the token it carries, not the 30 minutes a rain warning once got. A phone
        # offline for an hour during signup used to come back to nothing, having been told the
        # notification was on its way.
        ttl_seconds=settings.confirm_token_ttl_hours * 3600,
    )


def manage_link_message(settings: Settings, subscriber, token: str) -> OutboundMessage:
    """The single-use link to the settings (on the start page since D-67).

    The route into the settings when this browser has no device key (D-64's fallback). The token
    rides in the URL **fragment**, never sent to the server, so it cannot appear in a request log, a
    proxy's history or a Referer header - the leak F-4/F-8 describe for `?token=`. The page reads it
    from `location.hash`, trades it for a session, and erases it.
    """
    link = f"{settings.public_base_url.rstrip('/')}/#t={token}"
    minutes = settings.manage_link_ttl_minutes
    return OutboundMessage(
        to=subscriber.address,
        **push_keys(subscriber),
        # Short: Android truncates a title, and this one once lost its last word.
        subject="Einstellungen öffnen",
        # One line. It used to be a mail-shaped body: ten lines, two raw URLs nothing linkifies, a
        # "wenn du das nicht warst" warning one second after the reader pressed the button on their
        # own phone, and the subscriber's UUID legible on a lock screen.
        text=f"Zum Öffnen antippen. Gültig {minutes} Minuten, einmal benutzbar.\n",
        click_url=link,
        push_tag=MANAGE_TAG,
        # As long as the link is valid, and no longer: a settings link the push service held past
        # its own expiry would arrive already spent, which looks like the service being broken.
        ttl_seconds=minutes * 60,
    )


def deletion_receipt(settings: Settings, endpoint: str, keys: dict) -> OutboundMessage:
    """The last notification this browser ever gets from us, sent after its row is gone.

    Takes the endpoint and keys rather than the subscriber, because by the time it is sent there is
    no subscriber row to read them from. Deleting from the settings page also has the page call
    `PushSubscription.unsubscribe()`, so the browser stops holding a subscription nothing will ever
    post to. A subscriber who deletes by blocking notifications or clearing site data never
    receives this at all - the endpoint is already dead, which is how we find out.
    """
    return OutboundMessage(
        to=endpoint,
        **keys,
        subject="Regenwarnung gelöscht",
        # No URL and no sign-off: one thing to say. Somebody who wants back in opens the site.
        text="Deine Regenwarnung wurde gelöscht. Standort und Verlauf sind entfernt.\n",
        # A day: nothing here is time-critical - it is the artefact proving a deletion happened -
        # and there is no retry, because the row it would retry from is gone.
        ttl_seconds=24 * 3600,
    )


def _intensity(mm_per_5min: float) -> str:
    if mm_per_5min >= 1.0:
        return "kräftiger Regen"
    if mm_per_5min >= 0.35:
        return "Regen"
    return "leichter Regen"


def alert_message(
    session: Session,
    settings: Settings,
    subscriber,
    subscription,
    payload: dict,
) -> OutboundMessage:
    """The message the whole service exists to send."""
    tz = ZoneInfo(payload.get("timezone") or subscription.timezone)
    start = datetime.fromisoformat(payload["predicted_start_at"]).astimezone(tz)
    cycle_time = payload.get("cycle_time")
    observed = datetime.fromisoformat(cycle_time).astimezone(tz) if cycle_time else start
    lead = payload.get("lead_minutes") or 0
    peak = float(payload.get("peak_mm_5min") or 0.0)

    # "Es regnet jetzt" (D-69): rain began here without a warning before it. Says what is happening
    # and why there was no warning, so it does not read as the service having slept through it.
    rain_now = payload.get("kind") == "rain_now"
    if rain_now:
        text = f"""Bei dir hat gerade {_intensity(peak)} eingesetzt. Die Vorhersage hatte ihn nicht angekündigt.

Radarbild von {observed:%H:%M} Uhr, DWD.
"""
    else:
        text = f"""Voraussichtlich ab {start:%H:%M} Uhr (in etwa {lead} Minuten), {_intensity(peak)}.

Radarbild von {observed:%H:%M} Uhr, DWD-Vorhersage. Je kürzer die Vorwarnzeit, desto sicherer.
"""
    return OutboundMessage(
        to=subscriber.address,
        **push_keys(subscriber),
        subject="Es regnet jetzt" if rain_now else f"Regen in etwa {lead} Minuten",
        # The same slot as a warning: one rain message on screen at a time.
        push_tag=ALERT_TAG,
        text=text,
        # Where the reader lands when they tap the warning: the map, centred on the place the
        # warning was about, because the country view does not answer "is that shower coming to
        # me". A signed reference rather than the coordinates themselves: this link sits in a
        # notification list for good, and a screenshot of one should not be a home address. It
        # stops resolving after `locate_link_ttl_minutes` (tokens.py), and the map then opens where
        # it always did.
        click_url=(
            f"{settings.public_base_url.rstrip('/')}/"
            f"#l={locate_token(subscriber.id, settings.secret_key, settings.locate_link_ttl_minutes)}"
        ),
    )

"""The messages we send. Two of them at M3: confirmation, and the deletion receipt."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from rainalert.attribution import ATTRIBUTION
from rainalert.config import Settings
from rainalert.db.models import Channel
from rainalert.notify import MessageAction, OutboundMessage
from rainalert.tokens import locate_token, manage_request_token, unsubscribe_token

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
    """The RFC 8291 keys as kwargs, or nothing for an email subscriber.

    One helper rather than two attribute reads in five builders. `OutboundMessage` refuses a
    webpush message without them, so a builder that forgets this fails at construction - which is
    how the omission that made the whole channel undeliverable was found.
    """
    if subscriber is None or getattr(subscriber, "channel", None) != Channel.WEBPUSH:
        return {}
    return {"push_p256dh": subscriber.push_p256dh, "push_auth": subscriber.push_auth}


def confirmation_message(
    settings: Settings, to: str, token: str, *, channel: str = "email", subscriber=None
) -> OutboundMessage:
    """The double opt-in message, worded for the channel it goes out on.

    Same token, same endpoint, same one-use rule. What differs is only what the reader is being
    asked to believe: on email, that somebody typed *their address*, which may not have been them.
    On web push there is no such doubt - the browser issued the endpoint a moment ago, in response
    to a permission prompt the reader answered - so the message says what the tap is for rather
    than warning about a stranger.
    """
    # `#a=` means "confirm on open", `#t=` means "and wait for a click". The marker is chosen
    # here because this is the only place that knows the channel: the token is opaque, and the
    # server never sees the fragment, so the page cannot look the channel up for itself.
    #
    # Push gets `#a=`. SECURITY_REVIEW.md F-4 is what the extra click defends against, and every
    # actor it names is a *mail* scanner - SafeLinks, Proofpoint, Gmail's link handling. None of
    # them sits between this service and a notification on someone's phone, so on push the click
    # protects nothing and costs a step. Mail keeps it.
    marker = "t" if channel == "email" else "a"
    # Neither branch says "ohne Bestätigung wird nichts gespeichert" any more, on either channel.
    # It was not true: the pending row exists from the moment of signup, carrying the coordinates
    # and a salted IP hash, which is what `privacy.html` has always said. The same sentence was
    # removed from `index.html` for exactly this reason and left standing here - in the one artefact
    # the reader actually keeps - so a reader who declined to confirm was told nothing was stored
    # while something was, on a service whose privacy page cites Art. 6 Abs. 1 lit. a DSGVO. What
    # is true, and is now what both say, is that an unconfirmed signup is deleted after
    # `unconfirmed_purge_hours` - enforced by `purge_unconfirmed` in the ingest job.
    link = f"{settings.public_base_url.rstrip('/')}/confirm#{marker}={token}"
    if channel == "email":
        text = f"""Hallo,

jemand hat diese Adresse fuer eine Regenwarnung angemeldet.

Zum Bestaetigen bitte diesen Link oeffnen:
{link}

Der Link gilt {settings.confirm_token_ttl_hours} Stunden und kann nur einmal benutzt werden.

Wenn du das nicht warst, ignoriere diese Mail einfach - ohne Bestaetigung loeschen wir die
Anmeldung nach {settings.unconfirmed_purge_hours} Stunden und es werden keine weiteren Mails verschickt.

--
{ATTRIBUTION}
"""
    else:
        # No URL in the body. A notification body is plain text that no platform linkifies, so a
        # link printed here is one the reader can see and not open - worse than none, because it
        # looks like the way forward. Tapping the notification is the way forward, and that is
        # what `click_url` is.
        # Action first: Android shows one line collapsed, and the old first line was a
        # meta-statement about the notification rather than what to do with it. No licence footer
        # either - `alert_message` and `deletion_receipt` both drop it on push and this was the odd
        # one out, putting a copyright line under the most important notification in the flow. The
        # attribution is on every page of the site.
        text = f"""Zum Aktivieren antippen. Dann weißt du, dass die Warnungen bei dir ankommen.

Gültig {settings.confirm_token_ttl_hours} Stunden, einmal benutzbar. Ohne Bestätigung löschen wir
die Anmeldung nach {settings.unconfirmed_purge_hours} Stunden und es kommt nichts weiter.
"""
    return OutboundMessage(
        to=to,
        channel=channel,
        **push_keys(subscriber),
        # Same reason as the deletion receipt: a mail header on one channel, a notification title on
        # the other, and "bestaetigen" reads as a typo in a notification shade.
        subject="Regenwarnung bestaetigen" if channel == "email" else "Regenwarnung bestätigen",
        text=text,
        # Push has nowhere to put a link except here. Email ignores it and uses the body.
        click_url=link,
        # As long as the token it carries, not the 30 minutes a rain warning gets. A phone offline
        # for an hour during signup used to come back to nothing, having been told the notification
        # was on its way.
        ttl_seconds=settings.confirm_token_ttl_hours * 3600,
        headers={
            "From": settings.mail_from,
            # Tells well-behaved automation this is not a human conversation.
            "Auto-Submitted": "auto-generated",
        },
    )


def unsubscribe_url(settings: Settings, subscriber_id) -> str:
    """In the fragment (D-26), so the token cannot reach a request log."""
    return f"{settings.public_base_url.rstrip('/')}/unsubscribe#t={unsubscribe_token(subscriber_id, settings.secret_key)}"


def unsubscribe_line(settings: Settings, subscriber_id) -> str:
    """The way out, on every message that follows the confirmation.

    Every one of them, not just the alerts: somebody who wants to stop reaches for whichever
    message is in front of them, and a settings link that offers no exit is a message that says
    "you can change this" while hiding the one change they came for.

    Not on the confirmation itself - there is nothing to leave yet, and an unconfirmed signup
    deletes itself - and not on the deletion receipt, which is the last thing the channel ever
    gets.

    **Email only since D-45.** ntfy's clients linkified a bare URL, so this was tappable there
    without being a button. A web push notification body is plain text that nothing linkifies, so
    printing it would show the reader an exit they cannot take. The exit on push is the settings
    page, which carries "Abmelden und Daten loeschen" for exactly this reason, reached by the
    Einstellungen button on every warning. Not a destructive action button: one of those, on a
    notification that arrives often, is one mis-tap from an account nobody meant to delete.
    """
    return f"Abmelden: {unsubscribe_url(settings, subscriber_id)}"


def settings_action(settings: Settings, token: str) -> MessageAction:
    """The "Einstellungen" button that rides on every push we send.

    Tapping it POSTs the durable request token back to us and we send the ordinary magic link to
    the same browser. Two taps, neither of which opens anything: the service worker does the POST
    in the background, so the token never reaches a URL bar or a history entry.

    The button asks; it does not admit. That split is what lets the token be durable enough to
    sit in a notification the reader keeps (tokens.py).
    """
    return MessageAction(
        label="Einstellungen",
        url=f"{settings.public_base_url.rstrip('/')}/api/v1/manage/request",
        # Compact separators are no longer load-bearing - they were, while this had to survive
        # ntfy's comma-separated header grammar - but a payload has 4096 octets guaranteed and
        # nothing is gained by spending them on whitespace.
        body=json.dumps({"token": token}, separators=(",", ":")),
    )


def manage_link_message(
    settings: Settings,
    to: str,
    token: str,
    subscriber_id,
    *,
    channel: str = "email",
    subscriber=None,
) -> OutboundMessage:
    """The magic link to the settings page.

    The token rides in the URL **fragment**, not the query string, and that is the whole point of
    the shape. A fragment is never sent to the server, so it cannot appear in a request log, in a
    proxy's history or in a Referer header - which is exactly the leak F-4/F-8 describe for
    `?token=`. The page reads it from `location.hash`, trades it for a session, and erases it.
    """
    link = f"{settings.public_base_url.rstrip('/')}/manage#t={token}"
    minutes = settings.manage_link_ttl_minutes
    if channel == "email":
        text = f"""Hier geht es zu deinen Einstellungen:
{link}

Der Link gilt {minutes} Minuten und kann nur einmal benutzt werden.

Wenn du das nicht warst, ignoriere diese Nachricht - solange der Link nicht geöffnet wird,
ändert sich nichts.

{unsubscribe_line(settings, subscriber_id)}

--
{ATTRIBUTION}
"""
    else:
        # One line, because this is the only route a push subscriber has into their settings and it
        # arrives in a notification shade that shows one line collapsed and three expanded. It used
        # to send the mail body: ten lines, two raw URLs that nothing linkifies - including the very
        # unsubscribe URL `unsubscribe_line` refuses to print on push for exactly that reason - a
        # "wenn du das nicht warst" warning one second after the reader pressed the button on their
        # own phone, and a licence footer. The subscriber's UUID was legible on a lock screen.
        #
        # `click_url` already carries the link, so the body has nothing to carry.
        text = f"Zum Öffnen antippen. Gültig {minutes} Minuten, einmal benutzbar.\n"
    return OutboundMessage(
        to=to,
        channel=channel,
        **push_keys(subscriber),
        # Split by channel, which every other builder here already does and this one did not.
        # `aendern` is the ASCII spelling a mail header needs; in a notification shade it reads as a
        # typo, on the one notification that is a push subscriber's only route into their settings.
        # It is also shorter now, because Android truncates a title and this one was long enough to
        # lose its last word.
        subject=(
            "Regenwarnung: Einstellungen aendern" if channel == "email" else "Einstellungen öffnen"
        ),
        text=text,
        click_url=link,
        push_tag=MANAGE_TAG,
        # As long as the link is valid, and no longer: a settings link the push service held past
        # its own expiry would arrive already spent, which looks like the service being broken.
        ttl_seconds=minutes * 60,
        headers={"From": settings.mail_from, "Auto-Submitted": "auto-generated"},
    )


def deletion_receipt(
    settings: Settings, to: str, *, channel: str = "email", push: dict | None = None
) -> OutboundMessage:
    """Sent to the channel being deleted, as the last thing that channel ever receives.

    On web push it asks nothing of the reader. Deleting from the settings page has the page call
    `PushSubscription.unsubscribe()` as well, so the browser stops holding a subscription nothing
    will ever post to - which was not possible on ntfy, where only the subscriber could stop their
    app listening.

    There is no delete action on a notification, so there is no service-worker path to describe:
    `maxActions` is 2 and a destructive button on a message that arrives whenever it rains is one
    mis-tap from an account nobody meant to delete (see `alert_message`). A subscriber who deletes
    by blocking notifications or clearing site data never receives this receipt at all - the
    endpoint is already dead, which is how we find out.
    """
    if channel == "email":
        text = f"""Hallo,

deine Regenwarnung wurde geloescht. Adresse, Standort und Verlauf sind entfernt.

Du kannst dich jederzeit neu anmelden:
{settings.public_base_url.rstrip("/")}/

--
{ATTRIBUTION}
"""
    else:
        # No URL and no sign-off: a notification shows a couple of lines, and this one has one
        # thing to say. The re-signup link is omitted rather than printed unusably (see
        # unsubscribe_line) - somebody who wants back in opens the site they just came from.
        text = "Deine Regenwarnung wurde gelöscht. Standort und Verlauf sind entfernt.\n"
    return OutboundMessage(
        # A day, not the 30 minutes a rain warning gets. Nothing in this message is time-critical -
        # it is the artefact proving a deletion happened - and there is no retry, because the row it
        # would retry from is gone. A phone off-network for 40 minutes lost it silently.
        ttl_seconds=24 * 3600,
        to=to,
        channel=channel,
        **(push or {}),
        # The subject is a mail header for email - where a bare umlaut is a mojibake risk - and a
        # notification title for push, where "geloescht" reads as a typo. So it differs by channel.
        subject="Regenwarnung geloescht" if channel == "email" else "Regenwarnung gelöscht",
        text=text,
        headers={"From": settings.mail_from, "Auto-Submitted": "auto-generated"},
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

    unsubscribe = unsubscribe_url(settings, subscriber.id)
    is_push = subscriber.channel == Channel.WEBPUSH

    # The way out is printed only where it can be taken. See unsubscribe_line: on push this body
    # is plain text nothing linkifies, and the exit is the Einstellungen button below.
    exit_line = "" if is_push else f"\n{unsubscribe_line(settings, subscriber.id)}\n"
    # A push body also has no room for the provenance paragraph - a notification shows two or
    # three lines before it truncates, and the attribution is on every page of the site.
    if is_push:
        text = f"""Voraussichtlich ab {start:%H:%M} Uhr (in etwa {lead} Minuten), {_intensity(peak)}.

Radarbild von {observed:%H:%M} Uhr, DWD-Vorhersage. Je kürzer die Vorwarnzeit, desto sicherer.
"""
    else:
        text = f"""Es faengt bald an zu regnen.

Voraussichtlich ab {start:%H:%M} Uhr (in etwa {lead} Minuten), {_intensity(peak)}.

Grundlage: Radarvorhersage des DWD, Radarbild von {observed:%H:%M} Uhr.
Vorhersagen aendern sich - je kuerzer die Vorwarnzeit, desto sicherer.
{exit_line}
--
{ATTRIBUTION}
"""
    headers = {
        "From": settings.mail_from,
        "Auto-Submitted": "auto-generated",
        # A link, not RFC 8058 one-click. `List-Unsubscribe-Post` would promise a mail client it
        # may POST this URI, and two things follow from that promise: the client never runs the
        # page, so the token would have to sit in the query string where a log gets it (D-26),
        # and the handler would have to read it from there - which it does not, so the promise
        # was answered 400 for as long as it was made (D-33). Without the POST header the URI is
        # opened rather than posted, so it can be the same fragment link a person clicks.
        "List-Unsubscribe": f"<{unsubscribe}>",
    }
    if settings.mail_reply_to:
        headers["Reply-To"] = settings.mail_reply_to
    # The one button on a warning, and since D-45 the only durable route to the settings page:
    # a web push notification is gone the moment it is swiped, so the reader cannot go back and
    # find an earlier one. An alert is the message that reliably arrives again, which is why the
    # route lives here. Push only: the header is meaningless to a mail client, and email has the
    # settings form already.
    #
    # Deliberately not a second "Abmelden" button. `maxActions` is 2 so there is room, but a
    # destructive action on a notification that arrives whenever it rains is one mis-tap from an
    # account nobody meant to delete. Abmelden lives one tap further in, on the settings page.
    actions = ()
    if is_push:
        actions = (
            settings_action(
                settings,
                manage_request_token(
                    subscriber.id, settings.secret_key, settings.manage_request_ttl_days
                ),
            ),
        )

    return OutboundMessage(
        to=subscriber.address,
        channel=str(subscriber.channel),
        **push_keys(subscriber),
        subject=f"Regen in etwa {lead} Minuten",
        push_tag=ALERT_TAG,
        text=text,
        # On push this is where the reader lands when they tap the warning. The map, so the
        # first thing they see is the rain that is coming rather than a sign-up form - centred
        # on the place the warning was about, which it has to be told, because the country view
        # does not answer "is that shower coming to me".
        #
        # A signed reference rather than the coordinates themselves: this link sits in a
        # notification list for good, and a screenshot of one should not be a home address. It
        # stops resolving after `locate_link_ttl_minutes` (tokens.py), and the map then opens
        # where it always did.
        #
        # Push only. Email ignores `click_url`, and a mail body is forwarded far more often than
        # a notification is - there is no reason to put this where it travels furthest.
        click_url=(
            f"{settings.public_base_url.rstrip('/')}/"
            f"#l={locate_token(subscriber.id, settings.secret_key, settings.locate_link_ttl_minutes)}"
        ),
        actions=actions,
        headers=headers,
    )

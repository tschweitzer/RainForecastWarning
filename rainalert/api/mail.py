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
from rainalert.tokens import manage_request_token, unsubscribe_token


def confirmation_message(
    settings: Settings, to: str, token: str, *, channel: str = "email"
) -> OutboundMessage:
    """The double opt-in message, worded for the channel it goes out on.

    Same token, same endpoint, same one-use rule. What differs is only what the reader is being
    asked to believe: on email, that somebody typed *their address*, which may not have been
    them. On a push topic there is no such doubt - the topic did not exist until they asked for
    it, and it reached their phone - so the message says what the tap is for rather than warning
    about a stranger.
    """
    link = f"{settings.public_base_url.rstrip('/')}/confirm#t={token}"
    if channel == "email":
        text = f"""Hallo,

jemand hat diese Adresse fuer eine Regenwarnung angemeldet.

Zum Bestaetigen bitte diesen Link oeffnen:
{link}

Der Link gilt {settings.confirm_token_ttl_hours} Stunden und kann nur einmal benutzt werden.

Wenn du das nicht warst, ignoriere diese Mail einfach - ohne Bestaetigung wird nichts
gespeichert und es werden keine weiteren Mails verschickt.

--
{ATTRIBUTION}
"""
    else:
        text = f"""Diese Benachrichtigung beweist, dass die Warnungen dich erreichen.

Zum Aktivieren antippen, oder diesen Link oeffnen:
{link}

Gueltig {settings.confirm_token_ttl_hours} Stunden, einmal benutzbar. Ohne Bestaetigung wird
nichts gespeichert und es kommt nichts weiter.

--
{ATTRIBUTION}
"""
    return OutboundMessage(
        to=to,
        subject="Regenwarnung bestaetigen",
        text=text,
        # Push has nowhere to put a link except here. Email ignores it and uses the body.
        click_url=link,
        headers={
            "From": settings.mail_from,
            # Tells well-behaved automation this is not a human conversation.
            "Auto-Submitted": "auto-generated",
        },
    )


def settings_action(settings: Settings, token: str) -> MessageAction:
    """The "Einstellungen" button that rides on every push we send.

    Tapping it POSTs the durable request token back to us and we send the ordinary magic link to
    the same topic. Two taps, both inside the app, and the topic never has to be copied out of
    it - which was the whole of the old detour.

    The button asks; it does not admit. That split is what lets the token be durable enough to
    sit in a notification the reader keeps (tokens.py).
    """
    return MessageAction(
        label="Einstellungen",
        url=f"{settings.public_base_url.rstrip('/')}/api/v1/manage/request",
        # JSON, and JSON without a space in it. ntfy's header grammar splits parameters on the
        # comma, and its documented example passes a JSON body unquoted - which works only while
        # the JSON itself has no comma in it. One key, so it has none, and `separators` keeps it
        # that way rather than leaving it to json.dumps' defaults.
        body=json.dumps({"token": token}, separators=(",", ":")),
    )


def settings_anchor_message(settings: Settings, to: str, token: str) -> OutboundMessage:
    """Sent once, right after confirmation: the message the reader is asked to keep.

    A rain alert carries the same button, but a rain alert is transient - it is swiped away the
    moment it has been read. This one exists to be the stable entry point, and says so.

    It is push-only by design. A mailbox is something people can type from memory, so the
    settings form already serves email; a generated topic is not, which is the asymmetry this
    whole flow is about.
    """
    fallback = f"{settings.public_base_url.rstrip('/')}/manage#r={token}"
    text = f"""Alles eingerichtet. Ab jetzt melden wir uns, bevor es bei dir anfaengt zu regnen.

Behalte diese Nachricht am besten. Mit dem Knopf "Einstellungen" forderst du jederzeit einen
Link an, um Ort, Schwelle, Vorwarnzeit und Umkreis zu aendern - ohne dein Thema irgendwo
eintippen zu muessen.

Der Link kommt dann als neue Nachricht hier an und gilt {settings.manage_link_ttl_minutes} Minuten.

Falls dein Client keine Knoepfe anzeigt, geht es auch hierueber:
{fallback}

--
{ATTRIBUTION}
"""
    return OutboundMessage(
        to=to,
        subject="Regenwarnung ist aktiv",
        text=text,
        actions=(settings_action(settings, token),),
        headers={"From": settings.mail_from, "Auto-Submitted": "auto-generated"},
    )


def manage_link_message(settings: Settings, to: str, token: str) -> OutboundMessage:
    """The magic link to the settings page.

    The token rides in the URL **fragment**, not the query string, and that is the whole point of
    the shape. A fragment is never sent to the server, so it cannot appear in a request log, in a
    proxy's history or in a Referer header - which is exactly the leak F-4/F-8 describe for
    `?token=`. The page reads it from `location.hash`, trades it for a session, and erases it.
    """
    link = f"{settings.public_base_url.rstrip('/')}/manage#t={token}"
    minutes = settings.manage_link_ttl_minutes
    text = f"""Hier geht es zu deinen Einstellungen:
{link}

Der Link gilt {minutes} Minuten und kann nur einmal benutzt werden.

Wenn du das nicht warst, ignoriere diese Nachricht - solange der Link nicht geoeffnet wird,
aendert sich nichts.

--
{ATTRIBUTION}
"""
    return OutboundMessage(
        to=to,
        subject="Regenwarnung: Einstellungen aendern",
        text=text,
        click_url=link,
        headers={"From": settings.mail_from, "Auto-Submitted": "auto-generated"},
    )


def deletion_receipt(settings: Settings, to: str, *, channel: str = "email") -> OutboundMessage:
    """Sent to the channel being deleted, as the last thing that channel ever receives.

    On ntfy this is also the subscriber's cue to unsubscribe the topic in their app: we stop
    publishing, but only they can stop listening.
    """
    text = f"""Hallo,

deine Regenwarnung wurde geloescht. Adresse, Standort und Verlauf sind entfernt.
{"Dieses Thema kannst du jetzt in der App abbestellen - es kommt nichts mehr." if channel == "ntfy" else ""}
Du kannst dich jederzeit neu anmelden:
{settings.public_base_url.rstrip("/")}/

--
{ATTRIBUTION}
"""
    return OutboundMessage(
        to=to,
        subject="Regenwarnung geloescht",
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

    base = settings.public_base_url.rstrip("/")
    token = unsubscribe_token(subscriber.id, settings.secret_key)
    # In the fragment (D-26), so the token cannot reach a request log.
    unsubscribe_url = f"{base}/unsubscribe#t={token}"

    text = f"""Es faengt bald an zu regnen.

Voraussichtlich ab {start:%H:%M} Uhr (in etwa {lead} Minuten), {_intensity(peak)}.

Grundlage: Radarvorhersage des DWD, Radarbild von {observed:%H:%M} Uhr.
Vorhersagen aendern sich - je kuerzer die Vorwarnzeit, desto sicherer.

Abmelden: {unsubscribe_url}

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
        "List-Unsubscribe": f"<{unsubscribe_url}>",
    }
    if settings.mail_reply_to:
        headers["Reply-To"] = settings.mail_reply_to
    # The same button as the anchor notification, because the anchor is one swipe from being
    # gone and an alert is the one message that reliably arrives again. Push only: the header
    # is meaningless to a mail client, and email has the settings form already.
    actions = ()
    if subscriber.channel == Channel.NTFY:
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
        subject=f"Regen in etwa {lead} Minuten",
        text=text,
        # On push this is where the reader lands when they tap the warning. The map, so the
        # first thing they see is the rain that is coming rather than a sign-up form.
        click_url=f"{settings.public_base_url.rstrip('/')}/map",
        actions=actions,
        headers=headers,
    )

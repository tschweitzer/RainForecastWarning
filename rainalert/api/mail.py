"""The messages we send. Two of them at M3: confirmation, and the deletion receipt."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from rainalert.config import Settings
from rainalert.notify import OutboundMessage
from rainalert.tokens import unsubscribe_token

ATTRIBUTION = "Datenbasis: Deutscher Wetterdienst (DWD), Radarprodukt RV, CC BY 4.0"


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
    link = f"{settings.public_base_url.rstrip('/')}/confirm?token={token}"
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
    unsubscribe_url = f"{base}/unsubscribe?token={token}"

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
        # RFC 8058: lets a mail client offer one-click unsubscribe, which keeps complaints (and
        # therefore the sending domain's reputation) out of the spam button.
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    if settings.mail_reply_to:
        headers["Reply-To"] = settings.mail_reply_to
    return OutboundMessage(
        to=subscriber.address,
        subject=f"Regen in etwa {lead} Minuten",
        text=text,
        # On push this is where the reader lands when they tap the warning. The map, so the
        # first thing they see is the rain that is coming rather than a sign-up form.
        click_url=f"{settings.public_base_url.rstrip('/')}/map",
        headers=headers,
    )

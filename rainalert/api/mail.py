"""The messages we send. Two of them at M3: confirmation, and the deletion receipt."""

from __future__ import annotations

from rainalert.config import Settings
from rainalert.notify import OutboundMessage

ATTRIBUTION = "Datenbasis: Deutscher Wetterdienst (DWD), Radarprodukt RV, CC BY 4.0"


def confirmation_message(settings: Settings, to: str, token: str) -> OutboundMessage:
    link = f"{settings.public_base_url.rstrip('/')}/confirm?token={token}"
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
    return OutboundMessage(
        to=to,
        subject="Regenwarnung bestaetigen",
        text=text,
        headers={
            "From": settings.mail_from,
            # Tells well-behaved automation this is not a human conversation.
            "Auto-Submitted": "auto-generated",
        },
    )


def deletion_receipt(settings: Settings, to: str) -> OutboundMessage:
    text = f"""Hallo,

deine Regenwarnung wurde geloescht. Adresse, Standort und Verlauf sind entfernt.

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

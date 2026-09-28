"""subscribers: web push replaces the ntfy topic

The push channel stops being "a topic on somebody else's server" and becomes "this browser".
Three changes, one of which destroys data:

1. `address` widens from VARCHAR(320) to TEXT. 320 is the RFC 5321 bound on a mailbox; a W3C
   Push API endpoint has no documented ceiling. Measured 87-250 characters across FCM, Mozilla
   autopush and Apple, but an endpoint truncated on insert is a subscriber who can never be
   reached again and cannot be repaired, so the column stops having a length at all. In Postgres
   this is a catalogue-only change: varchar(n) -> text needs no table rewrite.

2. `push_p256dh` and `push_auth` are added, nullable. They hold the two values a browser hands
   over with its endpoint, and the payload is encrypted to them (RFC 8291). NULL for an email
   subscriber.

3. **Every existing ntfy subscriber is deleted.** There is no conversion: a topic is a name on a
   server the subscriber's app polls, an endpoint is a capability URL issued by their browser, and
   nothing on our side can turn one into the other. Leaving the rows would leave a `channel` value
   the application no longer understands, on rows nothing can ever deliver to. Those people have
   to subscribe again, which is a real cost and the reason this is called out here rather than
   done quietly. The delete cascades to subscriptions, notifications, auth_tokens and alert_states.

`channel` needs no constraint work: the enum was created with `native_enum=False` and no CHECK was
emitted, so the column is a plain VARCHAR(16) and "webpush" fits. Verified against the database
rather than assumed - `pg_constraint` has no row for it.

The downgrade is symmetric and equally destructive: webpush rows go, because they would not fit
back into VARCHAR(320) reliably and their channel value would be unknown to the older code.

Revision ID: f3b8c21e7a94
Revises: d5a1c7e93b42
Create Date: 2026-09-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8c21e7a94"
down_revision: str | Sequence[str] | None = "d5a1c7e93b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()

    # Say how much is being destroyed, in the migration output, before destroying it. A silent
    # DELETE in a migration is how you find out months later that someone stopped getting warnings.
    doomed = connection.execute(
        sa.text("SELECT count(*) FROM subscribers WHERE channel = 'ntfy'")
    ).scalar_one()
    if doomed:
        print(
            f"f3b8c21e7a94: deleting {doomed} ntfy subscriber(s) - a topic cannot become a push "
            f"endpoint, so they have to subscribe again"
        )
    connection.execute(sa.text("DELETE FROM subscribers WHERE channel = 'ntfy'"))

    op.alter_column(
        "subscribers",
        "address",
        existing_type=sa.String(length=320),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.add_column("subscribers", sa.Column("push_p256dh", sa.String(length=128), nullable=True))
    op.add_column("subscribers", sa.Column("push_auth", sa.String(length=64), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()

    stranded = connection.execute(
        sa.text("SELECT count(*) FROM subscribers WHERE channel = 'webpush'")
    ).scalar_one()
    if stranded:
        print(
            f"f3b8c21e7a94: deleting {stranded} webpush subscriber(s) - the older schema has no "
            f"column for their keys and no code that understands the channel"
        )
    connection.execute(sa.text("DELETE FROM subscribers WHERE channel = 'webpush'"))

    op.drop_column("subscribers", "push_auth")
    op.drop_column("subscribers", "push_p256dh")
    op.alter_column(
        "subscribers",
        "address",
        existing_type=sa.Text(),
        type_=sa.String(length=320),
        existing_nullable=False,
    )

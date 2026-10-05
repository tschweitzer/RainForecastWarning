"""subscribers: delete the ntfy rows f3b8c21e7a94 missed, and make channel case consistent

Revision ID: a9e4d2c71f05
Revises: f3b8c21e7a94
Create Date: 2026-10-05

`f3b8c21e7a94` was meant to delete every ntfy subscriber and deleted none. It ran
`DELETE FROM subscribers WHERE channel = 'ntfy'`, but the ORM column is
`Enum(Channel, native_enum=False)` with no `values_callable`, and SQLAlchemy persists a Python
enum's member *names* - so the rows said `NTFY`. Lowercase matched nothing, and the migration only
reported a count `if doomed:`, so it said nothing either. Its own comment warned that "a silent
DELETE in a migration is how you find out months later that someone stopped getting warnings".

That is how it was found. Once `Channel.NTFY` left the Python enum, the surviving row could not be
loaded at all, and `_persist` lazy-loads `subscription.subscriber` on the alert path - so whenever
rain approached that subscriber's location, the whole cycle raised, nobody was warned, and because
the cycle row is committed before evaluation it was never retried.

This does three things, all idempotent:

1. Upper-cases any lowercase channel value. Only `b7c31d9a4e10`'s `server_default="email"` could
   have written one - every ORM insert sets the value explicitly - but a lowercase row is as
   unreadable as an `NTFY` one, and converting is cheaper than finding out.
2. Deletes the ntfy subscribers, in either case. Cascades to their subscriptions, tokens, states,
   events, evaluations and notifications. A topic cannot become a push endpoint, so there is
   nothing to convert to - the same reasoning `f3b8c21e7a94` gave, applied to rows that exist.
3. Fixes the server default to `EMAIL`, so a raw insert without a channel produces a readable row.

`f3b8c21e7a94` is left as it is. It has run on the one database that ever had ntfy rows, editing an
applied migration rewrites history rather than repairing it, and this docstring is the record of
what it got wrong.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9e4d2c71f05"
down_revision: str | Sequence[str] | None = "f3b8c21e7a94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()

    lowercase = connection.execute(
        sa.text("SELECT count(*) FROM subscribers WHERE channel <> upper(channel)")
    ).scalar_one()
    # Always printed, including zero. The migration this repairs printed only when it found
    # something, and so said nothing on exactly the run where its WHERE clause was wrong.
    print(f"a9e4d2c71f05: {lowercase} subscriber(s) with a lowercase channel - upper-casing")
    connection.execute(
        sa.text("UPDATE subscribers SET channel = upper(channel) WHERE channel <> upper(channel)")
    )

    stranded = connection.execute(
        sa.text("SELECT count(*) FROM subscribers WHERE channel = 'NTFY'")
    ).scalar_one()
    print(
        f"a9e4d2c71f05: deleting {stranded} ntfy subscriber(s) left behind by f3b8c21e7a94 - "
        "the channel no longer exists and the rows cannot be loaded"
    )
    connection.execute(sa.text("DELETE FROM subscribers WHERE channel = 'NTFY'"))

    op.alter_column("subscribers", "channel", server_default="EMAIL")


def downgrade() -> None:
    # The deleted rows are not restorable - there was nothing they could have been restored *to*.
    # Only the default goes back, and the upper-casing stays, because lowercase is the unreadable
    # form and undoing a fix to it would only re-break the rows.
    op.alter_column("subscribers", "channel", server_default="email")

"""device_keys, and subscribers.last_seen_at

Revision ID: b6e2f0a4c813
Revises: a9e4d2c71f05
Create Date: 2026-10-09

DESIGN.md D-64 (docs/PLAN_DEVICE_KEY.md). A push subscriber's browser signs its settings requests
with a key whose public half is stored here, one per subscriber, deleted with the subscriber.

`last_seen_at` replaces "pressed the Einstellungen button" as the liveness job's sign that a person
is still reading: notifications no longer carry that button.

And the long-lived API tokens already issued to push subscribers are deleted: push subscribers are
no longer given one (PLAN_DEVICE_KEY.md §4.9), and one that was shown once at confirmation and
never needed since is the longest-lived credential they hold. Email subscribers keep theirs.

**Deploy order: this migration first, then the code.** The previous code runs fine against the new
schema, but the new code maps `subscribers.last_seen_at` and fails on every subscriber query until
the column exists. RUNBOOK §1b has the sequence (a targeted apply of the migrate job, the
migration, then the full apply). Found by code review, 2026-10-09.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e2f0a4c813"
down_revision: str | Sequence[str] | None = "a9e4d2c71f05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscribers", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "device_keys",
        sa.Column("id", sa.String(length=43), nullable=False),
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscriber_id"),
    )
    connection = op.get_bind()
    # Upper-case channel, lower-case purpose: the two enums are stored differently (models.py).
    doomed = connection.execute(
        sa.text(
            "DELETE FROM auth_tokens WHERE purpose = 'api' AND subscriber_id IN "
            "(SELECT id FROM subscribers WHERE channel = 'WEBPUSH')"
        )
    ).rowcount
    # Always printed, including zero (see a9e4d2c71f05 for why).
    print(f"b6e2f0a4c813: deleted {doomed} API token(s) of push subscribers")


def downgrade() -> None:
    # The deleted API tokens are not restored: they were never shown again after confirmation.
    op.drop_table("device_keys")
    op.drop_column("subscribers", "last_seen_at")

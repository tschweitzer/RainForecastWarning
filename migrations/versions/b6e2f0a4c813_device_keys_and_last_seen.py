"""device_keys, and subscribers.last_seen_at

Revision ID: b6e2f0a4c813
Revises: a9e4d2c71f05
Create Date: 2026-10-09

DESIGN.md D-64 (docs/PLAN_DEVICE_KEY.md). A push subscriber's browser signs its settings requests
with a key whose public half is stored here, one per subscriber, deleted with the subscriber.

`last_seen_at` replaces "pressed the Einstellungen button" as the liveness job's sign that a person
is still reading: notifications no longer carry that button.

Additive only, so either order of deploy is safe: the previous revision of the code never reads
either.
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


def downgrade() -> None:
    op.drop_table("device_keys")
    op.drop_column("subscribers", "last_seen_at")

"""Nieselregen from 0.008 mm: thresholds get a third decimal, and its subscribers follow it

Revision ID: f7c4a1e9b3d2
Revises: e5b2c8d4f1a6
Create Date: 2026-10-23

DESIGN.md D-80. The lowest band, and with it the lowest threshold a subscriber can choose, moved
from 0.01 to 0.008 mm per 5 minutes - the noise floor the operator chose with the debug noise cut.

- `subscriptions.threshold_mm_5min` becomes `numeric(6,3)`: `numeric(5,2)` would store 0.008 as
  0.01. Widening keeps every existing value exactly.
- Every subscription at exactly 0.01 - which is what choosing "Nieselregen" stored - moves to
  0.008, so it stays on the band it chose rather than turning into "eigener Wert" one thousandth
  stricter than the band. The settings page only ever offered the band values, so 0.01 means
  that choice; a value set through the API to another number is left as it is.

What it does to them: below 0.02 mm the sampler now reads DWD's thousandths, so a Nieselregen
subscriber is no longer warned about echoes under 0.008 - which today warn as 0.01. That is the
point of the change.

Deploy order is free: the previous code treats 0.008 like 0.01 (it compares against values that
are never below 0.01), and the new code before this migration only finds 0.01 rows.

Downgrade puts 0.008 back to 0.01 and the column back to two decimals, rounding anything else.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c4a1e9b3d2"
down_revision: str | Sequence[str] | None = "e5b2c8d4f1a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "subscriptions",
        "threshold_mm_5min",
        type_=sa.Numeric(6, 3),
        existing_type=sa.Numeric(5, 2),
        existing_nullable=False,
    )
    op.execute("UPDATE subscriptions SET threshold_mm_5min = 0.008 WHERE threshold_mm_5min = 0.01")


def downgrade() -> None:
    op.execute("UPDATE subscriptions SET threshold_mm_5min = 0.01 WHERE threshold_mm_5min < 0.01")
    op.alter_column(
        "subscriptions",
        "threshold_mm_5min",
        type_=sa.Numeric(5, 2),
        existing_type=sa.Numeric(6, 3),
        existing_nullable=False,
    )

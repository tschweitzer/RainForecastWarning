"""radar_cycles.wet_share

Revision ID: c7d1e5a9b2f4
Revises: b6e2f0a4c813
Create Date: 2026-10-14

DESIGN.md D-70. The plausibility check that replaced the blast radius compares each cycle's wet
share at t+0 with the previous cycle's, so that share has to outlive the job that computed it.
Nullable: cycles stored before this release have none, and the check skips the comparison when the
previous cycle has no value.

**Deploy order: this migration first, then the code.** The previous code runs fine against the new
schema, but the new code maps `radar_cycles.wet_share` and the ingest job fails on its first query
until the column exists. RUNBOOK §1b has the sequence.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d1e5a9b2f4"
down_revision: str | Sequence[str] | None = "b6e2f0a4c813"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("radar_cycles", sa.Column("wet_share", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("radar_cycles", "wet_share")

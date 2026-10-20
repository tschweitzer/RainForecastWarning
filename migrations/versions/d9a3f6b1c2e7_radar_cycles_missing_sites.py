"""radar_cycles.missing_sites

Revision ID: d9a3f6b1c2e7
Revises: c7d1e5a9b2f4
Create Date: 2026-10-20

DESIGN.md D-75. The radars DWD reports as missing from a cycle (`how/missing_nodes` in the HDF5
edition), so the settings page can say which one is down when a subscriber's location has no
data. Nullable: cycles from the binary edition, and every cycle stored before this release, have
no such list.

**Deploy order: this migration first, then the code**, as for c7d1e5a9b2f4: the new code maps the
column and the ingest job fails on its first query until it exists. RUNBOOK §1b has the sequence.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9a3f6b1c2e7"
down_revision: str | Sequence[str] | None = "c7d1e5a9b2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "radar_cycles", sa.Column("missing_sites", sa.ARRAY(sa.String(16)), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("radar_cycles", "missing_sites")

"""subscribers: channel + address, replacing email

Double opt-in was never really about email. It was about proving that the channel reaches the
person who asked - which is exactly as necessary for a push topic, and for the same reason. So
the subscriber's identity becomes (channel, address) and the confirmation goes out over whatever
channel that is, with one code path instead of a special case.

Existing rows are email subscribers by definition, so the data moves across rather than being
rewritten: email -> address, and the hash is recomputed because it now covers the channel too.
A hash that did not would let an ntfy topic spelled like a mailbox collide with that mailbox.

Revision ID: b7c31d9a4e10
Revises: 0d07df25ad95
Create Date: 2026-09-19

"""

from collections.abc import Sequence
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision: str = "b7c31d9a4e10"
down_revision: str | Sequence[str] | None = "0d07df25ad95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscribers",
        sa.Column(
            "channel",
            sa.Enum("email", "ntfy", name="channel", native_enum=False, length=16),
            nullable=False,
            server_default="email",
        ),
    )
    op.add_column("subscribers", sa.Column("address", sa.String(length=320), nullable=True))
    op.add_column("subscribers", sa.Column("address_hash", sa.LargeBinary(32), nullable=True))

    # Carry the data across. Every existing subscriber is an email subscriber, so the new hash
    # is computed here rather than in a migration-time import of application code - a migration
    # that calls into the app breaks the moment the app changes.
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, email FROM subscribers")).fetchall()
    for row in rows:
        address = (row.email or "").strip().lower()
        connection.execute(
            sa.text(
                "UPDATE subscribers SET address = :address, address_hash = :digest WHERE id = :id"
            ),
            {
                "address": address,
                "digest": sha256(f"email:{address}".encode()).digest(),
                "id": row.id,
            },
        )

    op.alter_column("subscribers", "address", nullable=False)
    op.alter_column("subscribers", "address_hash", nullable=False)
    op.create_index("ix_subscribers_address_hash", "subscribers", ["address_hash"], unique=True)
    op.drop_index("ix_subscribers_email_hash", table_name="subscribers")
    op.drop_column("subscribers", "email_hash")
    op.drop_column("subscribers", "email")


def downgrade() -> None:
    op.add_column("subscribers", sa.Column("email", sa.String(length=320), nullable=True))
    op.add_column("subscribers", sa.Column("email_hash", sa.LargeBinary(32), nullable=True))

    connection = op.get_bind()
    # Only email subscribers can come back; a push topic has nowhere to go in the old shape.
    # Refusing is better than inventing an address for them.
    remaining = connection.execute(
        sa.text("SELECT count(*) FROM subscribers WHERE channel <> 'email'")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{remaining} non-email subscriber(s) cannot be represented by the old schema; "
            "delete them or export them before downgrading"
        )

    rows = connection.execute(sa.text("SELECT id, address FROM subscribers")).fetchall()
    for row in rows:
        address = (row.address or "").strip().lower()
        connection.execute(
            sa.text("UPDATE subscribers SET email = :a, email_hash = :d WHERE id = :id"),
            {"a": address, "d": sha256(address.encode()).digest(), "id": row.id},
        )

    op.alter_column("subscribers", "email", nullable=False)
    op.alter_column("subscribers", "email_hash", nullable=False)
    op.create_index("ix_subscribers_email_hash", "subscribers", ["email_hash"], unique=True)
    op.drop_index("ix_subscribers_address_hash", table_name="subscribers")
    op.drop_column("subscribers", "address_hash")
    op.drop_column("subscribers", "address")
    op.drop_column("subscribers", "channel")

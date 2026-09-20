"""token_purpose: add 'manage', for the settings-page magic link

The link that opens the settings page is single use, so it is a stored token rather than a
signed one - and a stored token needs a purpose, or resolve_token would accept a confirmation
link as a settings link.

Downgrading deletes the manage tokens rather than trying to keep them. They live fifteen
minutes; a downgrade that waits that long loses nothing, and a value cannot be removed from a
postgres enum while any row still uses it.

Revision ID: c4f80ab21d63
Revises: b7c31d9a4e10
Create Date: 2026-09-20

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4f80ab21d63"
down_revision: str | Sequence[str] | None = "b7c31d9a4e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS so a database that has already been stamped by hand is not a hard failure.
    op.execute("ALTER TYPE token_purpose ADD VALUE IF NOT EXISTS 'manage'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum, so the type is rebuilt without it. The rows
    # using it go first, for the same reason.
    op.execute("DELETE FROM auth_tokens WHERE purpose = 'manage'")
    op.execute("ALTER TYPE token_purpose RENAME TO token_purpose_old")
    op.execute("CREATE TYPE token_purpose AS ENUM ('confirm', 'api', 'unsubscribe')")
    op.execute(
        "ALTER TABLE auth_tokens ALTER COLUMN purpose TYPE token_purpose "
        "USING purpose::text::token_purpose"
    )
    op.execute("DROP TYPE token_purpose_old")

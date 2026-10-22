"""push only: delete email subscribers and API tokens, default the channel columns

Revision ID: e5b2c8d4f1a6
Revises: d9a3f6b1c2e7
Create Date: 2026-10-22

DESIGN.md D-76: the service is push-only; email is gone from the code. What this does to the data:

- Every subscriber that is not a web push subscriber is deleted, and with it, by the existing
  cascades, its subscription, tokens, device key, alert state, events, notifications and
  evaluations. Matched case-insensitively on everything but WEBPUSH, because the column stores enum
  *names* and a deletion that matched the wrong case once left rows that crashed the alerting
  cycle (a9e4d2c71f05). Email was never enabled in production, so this is expected to delete
  nothing there; it is here so that no row the new code cannot represent can exist.
- API bearer tokens (`purpose = 'api'`) are deleted. Only email subscribers were issued them since
  b6e2f0a4c813, and nothing accepts them any more.
- Rate-limit rows that only email produced (`mail:transactional`, `subscribe:email:...`) go too.
- `subscribers.channel` and `notifications.channel` get the server default for web push. The new
  code no longer maps them, so it inserts rows without them; the old default for subscribers was
  'EMAIL', which would have labelled every new push subscriber as an email one.

**The columns themselves stay**, on purpose: the deploy runs this migration while the previous
code is still serving (RUNBOOK §1b), and that code reads them. Dropping them is safe in any later
release.

Downgrade restores the old defaults. The deleted rows are not restored - there is nothing to
restore them from, and an email subscriber could not be served by any code that would run here.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5b2c8d4f1a6"
down_revision: str | Sequence[str] | None = "d9a3f6b1c2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM subscribers WHERE upper(channel) <> 'WEBPUSH'")
    op.execute("DELETE FROM auth_tokens WHERE purpose = 'api'")
    op.execute(
        "DELETE FROM rate_limit_hits "
        "WHERE bucket = 'mail:transactional' OR bucket LIKE 'subscribe:email:%'"
    )
    op.execute("ALTER TABLE subscribers ALTER COLUMN channel SET DEFAULT 'WEBPUSH'")
    op.execute("ALTER TABLE notifications ALTER COLUMN channel SET DEFAULT 'webpush'")


def downgrade() -> None:
    op.execute("ALTER TABLE notifications ALTER COLUMN channel DROP DEFAULT")
    # Uppercase, as a9e4d2c71f05 left it.
    op.execute("ALTER TABLE subscribers ALTER COLUMN channel SET DEFAULT 'EMAIL'")

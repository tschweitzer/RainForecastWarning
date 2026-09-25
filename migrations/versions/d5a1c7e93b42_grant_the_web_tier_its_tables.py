"""grant the web tier read/write on the tables the ingest role owns

Revision ID: d5a1c7e93b42
Revises: c4f80ab21d63

Two database roles by design (F-6): the ingest job owns the schema and the web tier only reads
and writes rows. Nothing granted the second role anything, and in Postgres ownership is not
shared - so every table the migrations created belonged to `rainalert_ingest` and
`rainalert_api` could not touch it. The service came up healthy, the ingest job ran happily, and
the first request that read a table answered 500 with "permission denied for table
radar_cycles". `/readyz` could not report it either, because its own probe reads
`alembic_version` and hit the same wall.

Conditional on the role existing, so a single-user development database is unaffected: locally
the same account owns and uses everything and there is nothing to grant.

`ALTER DEFAULT PRIVILEGES` is the half that keeps this fixed. Without it the next migration
creates tables the web tier cannot see, and this bug returns one deploy later - somewhere else,
and just as quietly.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d5a1c7e93b42"
down_revision: str | Sequence[str] | None = "c4f80ab21d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The name Terraform gives the web tier's database user (infra/main.tf, google_sql_user.api).
APP_ROLE = "rainalert_api"


def upgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT USAGE ON SCHEMA public TO {APP_ROLE};
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};
            -- current_user, not a literal: whoever runs migrations owns what they create, and
            -- default privileges are recorded per granting role.
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}', current_user);
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}', current_user);
          END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}', current_user);
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}', current_user);
            REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE};
            REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE};
            REVOKE USAGE ON SCHEMA public FROM {APP_ROLE};
          END IF;
        END
        $$;
    """)

"""Issue #3062: Add partial unique email index + MCL sequence.

- Partial unique index on users.email for active verified users
  (prevents duplicate-account confusion from OAuth flows).
- PostgreSQL SEQUENCE for MCL sequence_number (race-free allocation).

Revision ID: a3062sec01
Revises: f537c8b67980
Create Date: 2026-03-16

"""

from collections.abc import Sequence as ABCSequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3062sec01"
down_revision: Union[str, ABCSequence[str], None] = "f537c8b67980"
branch_labels: Union[str, ABCSequence[str], None] = None
depends_on: Union[str, ABCSequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        # 1. Partial unique index on users.email — PostgreSQL only
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_verified_active
            ON users (email)
            WHERE is_active = 1 AND email_verified = 1 AND deleted_at IS NULL
            """
        )

        # 2. PostgreSQL SEQUENCE for MCL sequence_number
        op.execute("CREATE SEQUENCE IF NOT EXISTS mcl_sequence_number_seq")
        # Set current value to MAX(sequence_number) so new rows continue from there
        op.execute(
            """
            SELECT setval(
                'mcl_sequence_number_seq',
                COALESCE((SELECT MAX(sequence_number) FROM metadata_change_log), 0) + 1,
                false
            )
            """
        )
        # Set the column default to use the sequence
        op.execute(
            """
            ALTER TABLE metadata_change_log
            ALTER COLUMN sequence_number SET DEFAULT nextval('mcl_sequence_number_seq')
            """
        )
    else:
        # SQLite: partial indexes use a different syntax but SQLite supports them
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_verified_active
            ON users (email)
            WHERE is_active = 1 AND email_verified = 1 AND deleted_at IS NULL
            """
        )
        # SQLite has no SEQUENCE support; MCLRecorder handles allocation
        # via its fallback allocator with retry-on-collision.


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        op.execute("ALTER TABLE metadata_change_log ALTER COLUMN sequence_number DROP DEFAULT")
        op.execute("DROP SEQUENCE IF EXISTS mcl_sequence_number_seq")

    op.execute("DROP INDEX IF EXISTS uq_users_email_verified_active")

"""Migrate existing users to email_verified=1.

Revision ID: ev1434000001
Revises: u1234567890a
Create Date: 2026-02-13 00:00:00.000000

Issue #1434: Mark existing password users as email-verified so they are not
locked out when the email verification check is enabled at login.

Uses a cutoff timestamp to avoid a race condition with concurrent
registrations that should still require verification.
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ev1434000001"
down_revision: Union[str, Sequence[str], None] = "u1234567890a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Cutoff: users created before this migration should be grandfathered in.
# Using a fixed timestamp so the migration is reproducible.
CUTOFF = "2026-02-13T00:00:00"


def upgrade() -> None:
    """Set email_verified=1 for existing password users created before cutoff."""
    op.execute(
        text(
            """
            UPDATE users
            SET email_verified = 1
            WHERE email_verified = 0
              AND created_at < :cutoff
              AND primary_auth_method = 'password'
            """
        ).bindparams(cutoff=CUTOFF)
    )


def downgrade() -> None:
    """Revert: set email_verified back to 0 for grandfathered users.

    Note: This cannot distinguish originally-verified users from
    grandfathered ones, so it conservatively does nothing.
    """
    pass

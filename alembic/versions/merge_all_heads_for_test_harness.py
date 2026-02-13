"""Merge all heads into single head for migration test harness (Issue #1296).

Merges orphaned heads into a single head:
  - 62a871bc45de (add_path_and_session_lifecycle — orphaned by DAG cycle fix)
  - ev1434000001 (migrate_existing_users_email_verified — from #1455)

62a871bc45de became a head after fixing the DAG cycle where
928a619dabf4 (Oct 20) incorrectly depended on 62a871bc45de (Oct 29).
Restored 928a619dabf4.down_revision to its original value 'a16e1db56def'.

ev1434000001 branched off u1234567890a (add_user_model_tables) — a data
migration for email verification that was never merged into the main chain.

Revision ID: merge_all_heads_for_test_harness
Revises: merge_agent_keys_ns_views, 62a871bc45de, ev1434000001
Create Date: 2026-02-13
"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_all_heads_for_test_harness"
down_revision: Union[str, Sequence[str], None] = (
    "merge_agent_keys_ns_views",
    "62a871bc45de",
    "ev1434000001",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge heads — no schema changes."""
    pass


def downgrade() -> None:
    """Reverse merge — no schema changes."""
    pass

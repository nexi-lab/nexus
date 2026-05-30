"""Drop workspace stack tables (workspace_snapshots, context_branches, path_registrations).

The workspace stack — Issue #1264 (WorkspaceManager snapshots), Issue #1315
(ContextBranchService git-like branching), and Issue #189 (PathRegistration
backing workspace + memory registry HTTP) — was removed across the
preceding commits. Their three SQL tables go with them.

This migration also serves as the merge point for the four heads that
existed when the workspace removal landed:

  - remove_memory_unix_permissions
  - 2163141d44c5 (rename_content_hash_to_content_id)
  - d41d600929c4 (make_api_keys_zone_id_nullable_for_3785)
  - 62a871bc45de (add_path_and_session_lifecycle_to_*)

Idempotent: each DROP uses ``checkfirst=True``-style guards via
inspect() so re-running on a partially-cleaned DB is safe.

Revision ID: drop_workspace_stack_tables
Revises: remove_memory_unix_permissions, 2163141d44c5, d41d600929c4, 62a871bc45de
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_workspace_stack_tables"
down_revision: Union[str, Sequence[str], None] = (
    "remove_memory_unix_permissions",
    "2163141d44c5",
    "d41d600929c4",
    "62a871bc45de",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DROPPED_TABLES = ("workspace_snapshots", "context_branches", "path_registrations")


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    for table in _DROPPED_TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade() -> None:
    # No-op: the dropped tables backed services that no longer exist
    # (WorkspaceManager / ContextBranchService / PathRegistration HTTP).
    # Re-creating them on downgrade would leave orphan schema with no
    # writer.  Operators that need to roll back must restore from a
    # pre-drop backup.
    pass

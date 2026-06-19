"""Drop user_sessions table (orphan since #185).

The user_sessions SQL table was added by ``3c80651b81c6_add_session_
support_v0_5_0`` as the backing store for a CacheStore-backed session
service that was never wired (#185).  No ORM model was ever created
for the table and no ``src/`` code reads or writes it — the real
session backend caches the claim dict directly via raw CacheStoreABC
under ``auth:cache:*`` keys.  The preceding commits delete
``CacheSessionStore`` + ``SessionDTO``; this drop reclaims the orphan
schema.

Idempotent: ``DROP TABLE`` is guarded by ``inspect().get_table_names()``
so re-running on a partially-cleaned DB is safe.  All four
``idx_session_*`` indexes created alongside the table drop implicitly
with it.

Downgrade is intentionally a no-op — re-creating an orphan table with
no writer would be worse than restoring from a pre-drop backup.

Revision ID: drop_user_sessions_table
Revises: drop_workspace_stack_tables
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_user_sessions_table"
down_revision: Union[str, Sequence[str], None] = "drop_workspace_stack_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    if "user_sessions" in existing:
        op.drop_table("user_sessions")


def downgrade() -> None:
    # No-op: the dropped table backed a session service that was never
    # wired (CacheSessionStore was deleted as orphan).  Re-creating it
    # on downgrade would leave orphan schema with no writer.  Operators
    # that need to roll back must restore from a pre-drop backup.
    pass

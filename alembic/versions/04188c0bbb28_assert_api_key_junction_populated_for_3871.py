"""assert api_key_zones populated for #3871

Diagnostic migration. Fails loudly if any non-revoked api_keys row carries
a legacy ``zone_id`` column value but no corresponding ``api_key_zones``
junction row. Lands before the legacy zone_perms fallback is removed
(Task 8 of #3871) so data drift surfaces at upgrade time.

Truly zoneless admin keys (``zone_id IS NULL``) are exempt — they were
created post-Task-6 and have no zone access by design. Legacy
zone-scoped admin rows MUST be backfilled, otherwise the new auth code
silently reinterprets them as zoneless/global admins (privilege escalation).

Revision ID: 04188c0bbb28
Revises: d41d600929c4
Create Date: 2026-04-25 20:41:20.953795

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "04188c0bbb28"
down_revision: Union[str, Sequence[str], None] = "d41d600929c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        text(
            """
            SELECT k.key_id
            FROM api_keys k
            LEFT JOIN api_key_zones z ON z.key_id = k.key_id
            WHERE k.revoked = 0
              AND k.zone_id IS NOT NULL
              AND z.key_id IS NULL
            """
        )
    ).fetchall()
    if rows:
        sample = [r[0] for r in rows[:5]]
        raise RuntimeError(
            f"#3871 Phase 2 cleanup blocked: {len(rows)} live keys carry a legacy "
            f"zone_id with no api_key_zones row. Re-run the #3785 backfill before "
            f"upgrading. Sample key_ids: {sample}"
        )


def downgrade() -> None:
    pass  # assertion-only

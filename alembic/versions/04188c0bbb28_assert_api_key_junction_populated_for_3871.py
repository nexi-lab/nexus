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
    # Two failure modes (see #3871 rounds 3-5):
    #
    #   (A) Legacy zone-scoped row with NO matching junction zone — e.g.
    #       api_keys.zone_id='eng' but only api_key_zones('ops'). After Phase
    #       2 the legacy fallback is gone, so the key silently loses 'eng'.
    #
    #   (B) Non-admin row with no junction at all — round 4 made auth fail
    #       closed for empty-junction non-admin tokens (zoneless tokens are
    #       reserved for global admins). Without this check the upgrade
    #       succeeds but the token breaks on first authentication.
    rows = bind.execute(
        text(
            """
            SELECT k.key_id, k.zone_id, k.is_admin
            FROM api_keys k
            WHERE k.revoked = 0
              AND (
                (k.zone_id IS NOT NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM api_key_zones z
                   WHERE z.key_id = k.key_id AND z.zone_id = k.zone_id
                 ))
                OR (k.is_admin = 0
                    AND NOT EXISTS (
                      SELECT 1 FROM api_key_zones z WHERE z.key_id = k.key_id
                    ))
              )
            """
        )
    ).fetchall()
    if rows:
        sample = [(r[0], r[1], bool(r[2])) for r in rows[:5]]
        raise RuntimeError(
            f"#3871 Phase 2 cleanup blocked: {len(rows)} live keys would lose "
            f"access after the upgrade (legacy zone_id without a matching "
            f"api_key_zones row, or non-admin key with no junction zones). "
            f"Re-run the #3785 backfill before upgrading. "
            f"Sample (key_id, legacy zone_id, is_admin): {sample}"
        )


def downgrade() -> None:
    pass  # assertion-only

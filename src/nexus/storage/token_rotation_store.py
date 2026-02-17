"""Token rotation storage operations (Issue #1519).

Encapsulates SQLAlchemy queries for RFC 9700 refresh token rotation:
- Recording retired refresh tokens
- Detecting token reuse (replay attacks)
- Invalidating token families
- Pruning expired history

Extracted from ``server/auth/token_manager.py`` to keep the server layer
as a thin API adapter and push DB logic into the storage tier.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update

from nexus.storage.models import OAuthCredentialModel
from nexus.storage.models.refresh_token_history import RefreshTokenHistoryModel

logger = logging.getLogger(__name__)


class TokenRotationStore:
    """Storage-layer operations for refresh token rotation history.

    All methods accept an open SQLAlchemy session and do NOT commit —
    the caller is responsible for transaction boundaries.
    """

    def record_rotation(
        self,
        session: Any,
        credential_id: str,
        token_family_id: str,
        refresh_token_hash: str | None,
        rotation_counter: int,
        zone_id: str,
    ) -> None:
        """Record a retired refresh token in the history table."""
        if not refresh_token_hash:
            return

        history_entry = RefreshTokenHistoryModel(
            token_family_id=token_family_id,
            credential_id=credential_id,
            refresh_token_hash=refresh_token_hash,
            rotation_counter=rotation_counter,
            zone_id=zone_id,
            rotated_at=datetime.now(UTC),
        )
        session.add(history_entry)

    def detect_reuse(
        self,
        session: Any,
        token_family_id: str | None,
        refresh_token_hash: str,
    ) -> bool:
        """Check if a refresh token hash exists in rotation history.

        Returns True if the token was already rotated (replay attack).
        """
        if not token_family_id:
            return False
        stmt = select(RefreshTokenHistoryModel).where(
            RefreshTokenHistoryModel.token_family_id == token_family_id,
            RefreshTokenHistoryModel.refresh_token_hash == refresh_token_hash,
        )
        return session.execute(stmt).scalar_one_or_none() is not None

    def invalidate_family(
        self,
        session: Any,
        token_family_id: str | None,
    ) -> int:
        """Revoke all credentials in a token family.

        Returns the number of credentials revoked.
        """
        if not token_family_id:
            return 0
        now = datetime.now(UTC)
        result = session.execute(
            update(OAuthCredentialModel)
            .where(OAuthCredentialModel.token_family_id == token_family_id)
            .where(OAuthCredentialModel.revoked == 0)
            .values(revoked=1, revoked_at=now)
        )
        count = result.rowcount or 0
        if count > 0:
            logger.warning(
                "SECURITY: Invalidated %d credential(s) in family %s due to refresh token reuse",
                count,
                token_family_id,
            )
        return count

    def prune_history(
        self,
        session: Any,
        token_family_id: str | None,
        retention_days: int = 30,
    ) -> None:
        """Delete history entries older than retention period."""
        if not token_family_id:
            return
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        try:
            session.execute(
                delete(RefreshTokenHistoryModel).where(
                    RefreshTokenHistoryModel.token_family_id == token_family_id,
                    RefreshTokenHistoryModel.rotated_at < cutoff,
                )
            )
        except Exception:
            logger.warning("Failed to prune token history", exc_info=True)

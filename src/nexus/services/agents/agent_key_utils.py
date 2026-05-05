"""Storage-backed agent API key helpers for service-layer code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from nexus.storage.models import APIKeyModel


def determine_agent_key_expiration(user_id: str, session: Any) -> datetime:
    """Determine expiration date for an agent API key based on the owner's key."""
    stmt = (
        select(APIKeyModel)
        .where(
            APIKeyModel.user_id == user_id,
            APIKeyModel.revoked == 0,
            APIKeyModel.subject_type != "agent",
        )
        .order_by(APIKeyModel.created_at.desc())
    )
    owner_key = session.scalar(stmt)

    if owner_key and owner_key.expires_at:
        now = datetime.now(UTC)
        owner_expires: datetime = owner_key.expires_at
        if owner_expires.tzinfo is None:
            owner_expires = owner_expires.replace(tzinfo=UTC)
        if owner_expires > now:
            return owner_expires
        raise ValueError(
            f"Cannot generate API key for agent: Your API key has expired on "
            f"{owner_expires.isoformat()}. "
            "Please renew your API key before creating agent API keys."
        )
    return datetime.now(UTC) + timedelta(days=365)

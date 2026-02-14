"""Governance graph service — constraint CRUD + cache.

Issue #1359 Phase 3: Institutional constraints, policy-based checks,
dynamic constraint management with TTL cache.

Hot path: check_constraint() — <1ms cached, <5ms uncached.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.services.governance.models import (
    ConstraintCheckResult,
    ConstraintType,
    EdgeType,
    GovernanceEdge,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class GovernanceGraphService:
    """Manages governance constraints between agents.

    Provides:
        - CRUD for constraint edges in the governance graph
        - Fast constraint checking with TTL cache (<1ms cached)
        - Cache invalidation on mutations
    """

    _CACHE_TTL: float = 60.0  # seconds
    _CACHE_MAX_SIZE: int = 10_000

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        cache_ttl: float = 60.0,
    ) -> None:
        self._session_factory = session_factory
        self._CACHE_TTL = cache_ttl
        # Cache: (zone_id, from_agent, to_agent) -> (result, expires_at_monotonic)
        self._cache: dict[tuple[str, str, str], tuple[ConstraintCheckResult, float]] = {}

    async def add_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str,
        constraint_type: ConstraintType,
        reason: str = "",
    ) -> GovernanceEdge:
        """Add a governance constraint between two agents.

        Creates a CONSTRAINT edge in the governance graph.
        Invalidates the cache for this agent pair.
        """
        from nexus.services.governance.db_models import GovernanceEdgeModel

        edge_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        metadata: dict[str, object] = {"constraint_type": constraint_type, "reason": reason}

        model = GovernanceEdgeModel(
            id=edge_id,
            from_node=from_agent,
            to_node=to_agent,
            zone_id=zone_id,
            edge_type=EdgeType.CONSTRAINT,
            weight=0.0,
            metadata_json=json.dumps(metadata),
        )

        async with self._session_factory() as session, session.begin():
            session.add(model)

        # Invalidate cache
        self._invalidate(zone_id, from_agent, to_agent)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Added constraint %s: %s -> %s (zone=%s, type=%s)",
                edge_id,
                from_agent,
                to_agent,
                zone_id,
                constraint_type,
            )

        return GovernanceEdge(
            edge_id=edge_id,
            from_node=from_agent,
            to_node=to_agent,
            zone_id=zone_id,
            edge_type=EdgeType.CONSTRAINT,
            weight=0.0,
            metadata=metadata,
            created_at=now,
        )

    async def remove_constraint(self, edge_id: str) -> bool:
        """Remove a constraint by edge ID.

        Returns True if removed, False if not found.
        """
        from sqlalchemy import delete, select

        from nexus.services.governance.db_models import GovernanceEdgeModel

        async with self._session_factory() as session, session.begin():
            # Fetch for cache invalidation
            stmt = select(GovernanceEdgeModel).where(GovernanceEdgeModel.id == edge_id)
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()

            if model is None:
                return False

            zone_id = model.zone_id
            from_node = model.from_node
            to_node = model.to_node

            del_stmt = delete(GovernanceEdgeModel).where(GovernanceEdgeModel.id == edge_id)
            await session.execute(del_stmt)

        self._invalidate(zone_id, from_node, to_node)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Removed constraint %s", edge_id)

        return True

    async def check_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str,
    ) -> ConstraintCheckResult:
        """Check if there's a constraint between two agents.

        Hot path — uses TTL cache for <1ms lookups.
        """
        cache_key = (zone_id, from_agent, to_agent)
        now = time.monotonic()

        # Check cache
        cached = self._cache.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]

        # DB lookup
        result = await self._lookup_constraint(from_agent, to_agent, zone_id)

        # Cache result
        if len(self._cache) < self._CACHE_MAX_SIZE:
            self._cache[cache_key] = (result, now + self._CACHE_TTL)

        return result

    async def list_constraints(
        self,
        zone_id: str,
        agent_id: str | None = None,
    ) -> list[GovernanceEdge]:
        """List constraint edges, optionally filtered by agent."""
        from sqlalchemy import select

        from nexus.services.governance.db_models import GovernanceEdgeModel

        async with self._session_factory() as session:
            stmt = select(GovernanceEdgeModel).where(
                GovernanceEdgeModel.zone_id == zone_id,
                GovernanceEdgeModel.edge_type == EdgeType.CONSTRAINT,
            )

            if agent_id is not None:
                from sqlalchemy import or_

                stmt = stmt.where(
                    or_(
                        GovernanceEdgeModel.from_node == agent_id,
                        GovernanceEdgeModel.to_node == agent_id,
                    )
                )

            stmt = stmt.order_by(GovernanceEdgeModel.created_at.desc())
            result = await session.execute(stmt)
            models = result.scalars().all()

            return [self._model_to_edge(m) for m in models]

    async def update_constraint(
        self,
        edge_id: str,
        constraint_type: ConstraintType | None = None,
        reason: str | None = None,
    ) -> GovernanceEdge | None:
        """Update a constraint. Returns updated edge or None if not found."""
        from sqlalchemy import select

        from nexus.services.governance.db_models import GovernanceEdgeModel

        async with self._session_factory() as session, session.begin():
            stmt = select(GovernanceEdgeModel).where(GovernanceEdgeModel.id == edge_id)
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None

            metadata: dict[str, object] = {}
            if model.metadata_json:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    metadata = json.loads(model.metadata_json)

            if constraint_type is not None:
                metadata["constraint_type"] = constraint_type
            if reason is not None:
                metadata["reason"] = reason

            model.metadata_json = json.dumps(metadata)
            await session.flush()

            # Invalidate cache
            self._invalidate(model.zone_id, model.from_node, model.to_node)

            return self._model_to_edge(model)

    async def _lookup_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str,
    ) -> ConstraintCheckResult:
        """Look up constraint from database."""
        from sqlalchemy import select

        from nexus.services.governance.db_models import GovernanceEdgeModel

        async with self._session_factory() as session:
            stmt = select(GovernanceEdgeModel).where(
                GovernanceEdgeModel.zone_id == zone_id,
                GovernanceEdgeModel.from_node == from_agent,
                GovernanceEdgeModel.to_node == to_agent,
                GovernanceEdgeModel.edge_type == EdgeType.CONSTRAINT,
            )
            result = await session.execute(stmt)
            model = result.scalars().first()

        if model is None:
            return ConstraintCheckResult(allowed=True)

        metadata: dict[str, object] = {}
        if model.metadata_json:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                metadata = json.loads(model.metadata_json)

        ct_str = str(metadata.get("constraint_type", ConstraintType.BLOCK))
        try:
            ct = ConstraintType(ct_str)
        except ValueError:
            ct = ConstraintType.BLOCK

        reason = str(metadata.get("reason", ""))

        return ConstraintCheckResult(
            allowed=False,
            constraint_type=ct,
            reason=reason,
            edge_id=model.id,
        )

    def _invalidate(self, zone_id: str, from_agent: str, to_agent: str) -> None:
        """Invalidate cache for an agent pair."""
        self._cache.pop((zone_id, from_agent, to_agent), None)
        # Also invalidate reverse direction
        self._cache.pop((zone_id, to_agent, from_agent), None)

    def clear_cache(self) -> None:
        """Clear all cached constraint lookups (for testing)."""
        self._cache.clear()

    @staticmethod
    def _model_to_edge(model: Any) -> GovernanceEdge:
        """Convert GovernanceEdgeModel to domain GovernanceEdge."""
        metadata: dict[str, object] = {}
        if hasattr(model, "metadata_json") and model.metadata_json:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                metadata = json.loads(model.metadata_json)

        return GovernanceEdge(
            edge_id=model.id,
            from_node=model.from_node,
            to_node=model.to_node,
            zone_id=model.zone_id,
            edge_type=EdgeType(model.edge_type),
            weight=model.weight,
            metadata=metadata,
            created_at=model.created_at,
        )

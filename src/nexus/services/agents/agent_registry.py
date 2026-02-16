"""Agent registry with lifecycle state machine (Agent OS Phase 1, Issues #1240, #1588, #1589).

Consolidates all agent identity and lifecycle logic into a single class:
- Registration and unregistration
- State transitions with strict allowlist validation
- Session generation counter (increments on new session only)
- Optimistic locking via generation counter (cross-DB compatible)
- Heartbeat buffering via composed HeartbeatBuffer (Issue #1589)
- Queries: list by zone, owner, stale detection

Replaces scattered agent logic from agents.py and entity_registry.py
agent operations (Decision #5A). agent_provisioning.py stays separate
since it uses the NexusFS API layer, not raw registry operations.

Design decisions:
    - #2A: Generation increments on new session only (→ CONNECTED)
    - #5A: AgentRegistry consolidates all agent logic
    - #8A: Strict allowlist table for valid transitions
    - #13A: In-memory heartbeat with batch flush (via HeartbeatBuffer)
    - #16B: Optimistic locking via generation counter

References:
    - AGENT-OS-DEEP-RESEARCH.md Part 11 (Final Architecture)
    - Issue #1240: AgentRecord with session generation counter and state machine
    - Issue #1589: Extract HeartbeatBuffer from AgentRegistry (SRP)
"""

from __future__ import annotations

import json
import logging
import threading
import types
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
from cachetools import TTLCache
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from nexus.core.heartbeat_buffer import HeartbeatBuffer
from nexus.services.agents.agent_record import (
    AgentRecord,
    AgentState,
    is_new_session,
    validate_transition,
)
from nexus.storage.models import AgentRecordModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


def _safe_json_loads(raw: str | None, field_name: str, agent_id: str) -> Any:
    """Safely deserialize a JSON text column, returning a typed default on failure.

    Returns {} for 'agent_metadata', [] for all other fields (e.g. 'context_manifest').
    """
    default: Any = {} if field_name == "agent_metadata" else []
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("[AGENT-REG] Corrupt %s for agent %s", field_name, agent_id)
        return default


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed by the allowlist."""

    def __init__(self, agent_id: str, current: AgentState, target: AgentState) -> None:
        self.agent_id = agent_id
        self.current = current
        self.target = target
        super().__init__(
            f"Invalid transition for agent '{agent_id}': {current.value} -> {target.value}"
        )


class StaleAgentError(Exception):
    """Raised when optimistic locking detects a stale generation."""

    def __init__(self, agent_id: str, expected_generation: int) -> None:
        self.agent_id = agent_id
        self.expected_generation = expected_generation
        super().__init__(
            f"Stale generation for agent '{agent_id}': "
            f"expected generation {expected_generation} but record has changed"
        )


# Sentinel for cache miss (distinguishes "agent not found" from "not cached")
_CACHE_MISS = object()


class AgentRegistry:
    """Agent registry with lifecycle state machine and session generation counters.

    Heartbeat buffering is delegated to :class:`HeartbeatBuffer` (Issue #1589).
    Database operations use session-per-operation pattern (no held sessions).

    Args:
        session_factory: SQLAlchemy sessionmaker for database access.
        entity_registry: Optional EntityRegistry for backward compatibility bridge.
        flush_interval: Seconds between heartbeat buffer flushes (default: 60).
        cache_maxsize: Max entries in the get() TTLCache (default: 5000).
        cache_ttl: TTL in seconds for cached records (default: 10).
        max_buffer_size: Hard cap on heartbeat buffer entries (default: 50_000).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        entity_registry: Any = None,
        flush_interval: int = 60,
        cache_maxsize: int = 5000,
        cache_ttl: int = 10,
        max_buffer_size: int = 50_000,
    ) -> None:
        self._session_factory = session_factory
        self._entity_registry = entity_registry
        self._known_agents: TTLCache[str, bool] = TTLCache(maxsize=10_000, ttl=3600)
        self._lock = threading.Lock()  # protects _known_agents only
        # TTLCache for get() lookups to avoid per-request DB hits.
        # cachetools.TTLCache is NOT thread-safe — all access must be
        # synchronized via _cache_lock (see cachetools docs & issue #294).
        self._cache_lock = threading.Lock()
        self._record_cache: TTLCache[str, AgentRecord | None] = TTLCache(
            maxsize=cache_maxsize,
            ttl=cache_ttl,
        )
        # Heartbeat buffer composed via DI (Issue #1589)
        self._heartbeat_buffer = HeartbeatBuffer(
            flush_callback=self._flush_to_db,
            flush_interval=flush_interval,
            max_buffer_size=max_buffer_size,
        )

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        """Create a session from the factory with auto-commit/rollback."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def register(
        self,
        agent_id: str,
        owner_id: str,
        zone_id: str | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        capabilities: list[str] | None = None,
        context_manifest: list[dict[str, Any]] | None = None,
    ) -> AgentRecord:
        """Register a new agent. Returns existing record if agent_id already exists.

        New agents start in UNKNOWN state with generation 0.
        Handles concurrent registration via INSERT ON CONFLICT.

        Args:
            agent_id: Unique agent identifier.
            owner_id: User ID who owns this agent.
            zone_id: Zone/organization ID for multi-zone isolation.
            name: Human-readable display name.
            metadata: Arbitrary agent metadata.
            capabilities: Optional list of agent capabilities for discovery
                (e.g. ["search", "analyze", "code"]). Stored in metadata.
            context_manifest: Optional list of context source dicts for
                deterministic pre-execution (Issue #1341/1427).

        Returns:
            AgentRecord snapshot of the registered agent.

        Raises:
            ValueError: If agent_id or owner_id is empty.
        """
        if not agent_id:
            raise ValueError("agent_id is required")
        if not owner_id:
            raise ValueError("owner_id is required")

        # Merge capabilities into metadata (Issue #1210)
        if capabilities:
            metadata = dict(metadata) if metadata else {}
            metadata["capabilities"] = list(capabilities)

        with self._get_session() as session:
            # Check for existing
            existing = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if existing is not None:
                record = self._model_to_record(existing)
                return record

            now = datetime.now(UTC)
            model = AgentRecordModel(
                agent_id=agent_id,
                owner_id=owner_id,
                zone_id=zone_id,
                name=name,
                state=AgentState.UNKNOWN.value,
                generation=0,
                last_heartbeat=None,
                agent_metadata=json.dumps(metadata) if metadata else None,
                context_manifest=json.dumps(context_manifest) if context_manifest else None,
                created_at=now,
                updated_at=now,
            )
            try:
                session.add(model)
                session.flush()
            except IntegrityError:
                # Concurrent insert — re-read the winner's record
                session.rollback()
                existing = session.execute(
                    select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
                ).scalar_one()
                record = self._model_to_record(existing)
                return record

            record = self._model_to_record(model)

        # Track known agents for heartbeat fast-path
        with self._lock:
            self._known_agents[agent_id] = True

        # Bridge: also register in entity_registry if available
        if self._entity_registry is not None:
            try:
                entity_metadata = {}
                if name:
                    entity_metadata["name"] = name
                self._entity_registry.register_entity(
                    entity_type="agent",
                    entity_id=agent_id,
                    parent_type="user",
                    parent_id=owner_id,
                    entity_metadata=entity_metadata if entity_metadata else None,
                )
            except Exception:
                logger.error(
                    "[AGENT-REG] Bridge: failed to register %s in entity_registry",
                    agent_id,
                    exc_info=True,
                )
                raise

        logger.debug("[AGENT-REG] Registered agent %s (owner=%s)", agent_id, owner_id)
        return record

    def get(self, agent_id: str) -> AgentRecord | None:
        """Get an agent record by ID.

        Uses a TTLCache to avoid per-request DB hits. Cache is invalidated
        on transition() and unregister(). All cache access is synchronized
        via _cache_lock (cachetools.TTLCache is NOT thread-safe).

        Args:
            agent_id: Agent identifier.

        Returns:
            AgentRecord if found, None otherwise.
        """
        with self._cache_lock:
            cached = self._record_cache.get(agent_id, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cast("AgentRecord | None", cached)

        with self._get_session() as session:
            model = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                with self._cache_lock:
                    self._record_cache[agent_id] = None
                return None

            record = self._model_to_record(model)

        with self._cache_lock:
            self._record_cache[agent_id] = record
        return record

    def transition(
        self,
        agent_id: str,
        target_state: AgentState,
        expected_generation: int | None = None,
    ) -> AgentRecord:
        """Transition an agent to a new state with optimistic locking.

        Validates the transition against the strict allowlist (Decision #8A).
        If transitioning TO CONNECTED from a non-CONNECTED state, the generation
        counter increments (Decision #2A — new session only).

        Uses WHERE generation = expected_generation AND state = current_state
        for compare-and-swap semantics (Decision #16B).

        Args:
            agent_id: Agent identifier.
            target_state: Desired target state.
            expected_generation: Expected generation for optimistic locking.
                If None, locking check is skipped (state CAS still applies).

        Returns:
            New AgentRecord snapshot after transition.

        Raises:
            ValueError: If agent not found.
            InvalidTransitionError: If transition is not allowed.
            StaleAgentError: If expected_generation doesn't match (concurrent modification).
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                raise ValueError(f"Agent '{agent_id}' not found")

            current_state = AgentState(model.state)

            # Validate transition
            if not validate_transition(current_state, target_state):
                raise InvalidTransitionError(agent_id, current_state, target_state)

            # Compute new generation
            new_generation = model.generation
            if is_new_session(current_state, target_state):
                new_generation = model.generation + 1

            now = datetime.now(UTC)

            # Build WHERE clause with state CAS to prevent TOCTOU races
            stmt = (
                update(AgentRecordModel)
                .where(AgentRecordModel.agent_id == agent_id)
                .where(AgentRecordModel.state == current_state.value)
                .values(
                    state=target_state.value,
                    generation=new_generation,
                    updated_at=now,
                )
            )

            # Add generation guard for optimistic locking
            if expected_generation is not None:
                stmt = stmt.where(AgentRecordModel.generation == expected_generation)

            rows_updated: int = getattr(session.execute(stmt), "rowcount", 0)
            session.flush()

            if rows_updated == 0:
                if expected_generation is not None:
                    raise StaleAgentError(agent_id, expected_generation)
                # State changed between SELECT and UPDATE — re-read and report
                refreshed = session.execute(
                    select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
                ).scalar_one()
                actual_state = AgentState(refreshed.state)
                raise InvalidTransitionError(agent_id, actual_state, target_state)

            # Build record from known values (no re-read needed)
            metadata_dict = _safe_json_loads(model.agent_metadata, "agent_metadata", agent_id)
            manifest_list = _safe_json_loads(model.context_manifest, "context_manifest", agent_id)

            record = AgentRecord(
                agent_id=model.agent_id,
                owner_id=model.owner_id,
                zone_id=model.zone_id,
                name=model.name,
                state=target_state,
                generation=new_generation,
                last_heartbeat=model.last_heartbeat,
                metadata=types.MappingProxyType(metadata_dict),
                created_at=model.created_at,
                updated_at=now,
                context_manifest=tuple(manifest_list),
            )

        # Invalidate cache after transition
        with self._cache_lock:
            self._record_cache.pop(agent_id, None)

        logger.debug(
            "[AGENT-REG] Transition %s: %s -> %s (gen %d -> %d)",
            agent_id,
            current_state.value,
            target_state.value,
            model.generation,
            new_generation,
        )

        # Structured log for observability when agent connects with manifest
        if target_state is AgentState.CONNECTED and record.context_manifest:
            logger.info(
                "[AGENT-REG] Agent %s connected with %d manifest sources",
                agent_id,
                len(record.context_manifest),
            )

        return record

    def heartbeat(self, agent_id: str) -> None:
        """Record a heartbeat for an agent.

        Writes to the composed HeartbeatBuffer (Decision #13A). The buffer
        is flushed to DB when the flush_interval elapses. This reduces write
        amplification from frequent heartbeats.

        On the first call for a given agent_id, verifies existence via DB.
        Subsequent calls use a TTLCache of known agent IDs for fast-path
        (single-acquisition, Decision #2B).

        Args:
            agent_id: Agent identifier.

        Raises:
            ValueError: If agent not found.
        """
        # Single-acquisition fast path for known agents
        with self._lock:
            known = agent_id in self._known_agents

        if not known:
            record = self.get(agent_id)
            if record is None:
                raise ValueError(f"Agent '{agent_id}' not found")
            with self._lock:
                self._known_agents[agent_id] = True

        self._heartbeat_buffer.record(agent_id)

    def flush_heartbeats(self) -> int:
        """Flush the heartbeat buffer to the database.

        Returns:
            Number of heartbeats flushed.
        """
        return self._heartbeat_buffer.flush()

    def _flush_to_db(self, buffer: dict[str, datetime]) -> int:
        """Flush a buffer snapshot to the database.

        Used as the flush_callback for HeartbeatBuffer. The buffer handles
        restore-on-failure; this method only does the DB write.

        Args:
            buffer: Mapping of agent_id -> heartbeat timestamp to flush.

        Returns:
            Number of heartbeats flushed.
        """
        if not buffer:
            return 0

        params = [
            {"aid": agent_id, "ts": heartbeat_time} for agent_id, heartbeat_time in buffer.items()
        ]

        with self._get_session() as session:
            conn = session.connection()
            table = cast("sa.Table", AgentRecordModel.__table__)
            stmt = (
                update(table)
                .where(table.c.agent_id == sa.bindparam("aid"))
                .values(
                    last_heartbeat=sa.bindparam("ts"),
                    updated_at=sa.bindparam("ts"),
                )
            )
            conn.execute(stmt, params)

        flushed = len(params)
        logger.debug("[AGENT-REG] Flushed %d heartbeats to DB (batch)", flushed)
        return flushed

    def list_by_zone(
        self,
        zone_id: str,
        state: AgentState | None = None,
    ) -> list[AgentRecord]:
        """List agents in a zone, optionally filtered by state.

        Args:
            zone_id: Zone identifier.
            state: Optional state filter.

        Returns:
            List of AgentRecord snapshots.
        """
        with self._get_session() as session:
            stmt = select(AgentRecordModel).where(AgentRecordModel.zone_id == zone_id)
            if state is not None:
                stmt = stmt.where(AgentRecordModel.state == state.value)

            models = list(session.execute(stmt).scalars().all())
            return [self._model_to_record(m) for m in models]

    def list_by_owner(self, owner_id: str) -> list[AgentRecord]:
        """List agents owned by a user.

        Args:
            owner_id: User identifier.

        Returns:
            List of AgentRecord snapshots.
        """
        with self._get_session() as session:
            models = list(
                session.execute(
                    select(AgentRecordModel).where(AgentRecordModel.owner_id == owner_id)
                )
                .scalars()
                .all()
            )
            return [self._model_to_record(m) for m in models]

    def validate_ownership(self, agent_id: str, owner_id: str) -> bool:
        """Check if an agent belongs to a user.

        Args:
            agent_id: Agent identifier.
            owner_id: Expected owner user ID.

        Returns:
            True if agent exists and is owned by the specified user.
        """
        record = self.get(agent_id)
        if record is None:
            return False
        return record.owner_id == owner_id

    def unregister(self, agent_id: str) -> bool:
        """Unregister an agent, removing its record.

        Also removes from entity_registry if bridge is configured.

        Args:
            agent_id: Agent identifier.

        Returns:
            True if the agent was removed, False if not found.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                return False

            session.delete(model)

        # Bridge: also remove from entity_registry
        if self._entity_registry is not None:
            try:
                self._entity_registry.delete_entity("agent", agent_id)
            except Exception:
                logger.error(
                    "[AGENT-REG] Bridge: failed to remove %s from entity_registry",
                    agent_id,
                    exc_info=True,
                )
                raise

        # Remove from heartbeat buffer, known agents, and record cache
        self._heartbeat_buffer.remove(agent_id)
        with self._lock:
            self._known_agents.pop(agent_id, None)
        with self._cache_lock:
            self._record_cache.pop(agent_id, None)

        logger.debug("[AGENT-REG] Unregistered agent %s", agent_id)
        return True

    def update_manifest(
        self,
        agent_id: str,
        manifest: list[dict[str, Any]],
    ) -> AgentRecord:
        """Replace the context manifest for an existing agent.

        Args:
            agent_id: Agent identifier.
            manifest: List of context source dicts (serialized ContextSource models).

        Returns:
            Updated AgentRecord snapshot.

        Raises:
            ValueError: If agent not found.
        """
        with self._get_session() as session:
            # Verify agent exists
            existing = session.execute(
                select(AgentRecordModel.agent_id).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if existing is None:
                raise ValueError(f"Agent '{agent_id}' not found")

            now = datetime.now(UTC)
            stmt = (
                update(AgentRecordModel)
                .where(AgentRecordModel.agent_id == agent_id)
                .values(
                    context_manifest=json.dumps(manifest),
                    updated_at=now,
                )
            )
            session.execute(stmt)
            session.flush()

            # Re-read for immutable snapshot
            refreshed = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one()
            record = self._model_to_record(refreshed)

        # Invalidate cache
        with self._cache_lock:
            self._record_cache.pop(agent_id, None)

        logger.debug(
            "[AGENT-REG] Updated manifest for agent %s (%d sources)",
            agent_id,
            len(manifest),
        )
        return record

    def detect_stale(self, threshold_seconds: int = 300) -> list[AgentRecord]:
        """Find CONNECTED agents with stale heartbeats.

        An agent is stale if its last_heartbeat is older than the threshold
        or if it has never heartbeated while in CONNECTED state.

        Excludes agents that have recent heartbeats in the in-memory buffer
        (not yet flushed to DB).

        Args:
            threshold_seconds: Seconds since last heartbeat to consider stale.

        Returns:
            List of stale AgentRecord snapshots.
        """
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(seconds=threshold_seconds)

        # Check in-memory buffer for recent heartbeats not yet flushed
        recently_heartbeated = self._heartbeat_buffer.recently_heartbeated(cutoff)

        with self._get_session() as session:
            stmt = (
                select(AgentRecordModel)
                .where(AgentRecordModel.state == AgentState.CONNECTED.value)
                .where(
                    (AgentRecordModel.last_heartbeat.is_(None))
                    | (AgentRecordModel.last_heartbeat < cutoff)
                )
            )
            models = list(session.execute(stmt).scalars().all())
            records = [self._model_to_record(m) for m in models]

        # Exclude agents with recent in-memory heartbeats
        if recently_heartbeated:
            records = [r for r in records if r.agent_id not in recently_heartbeated]

        return records

    @staticmethod
    def _model_to_record(model: AgentRecordModel) -> AgentRecord:
        """Convert ORM model to frozen dataclass.

        Never returns mutable ORM objects — always creates a new immutable snapshot.

        Args:
            model: SQLAlchemy ORM model instance.

        Returns:
            Frozen AgentRecord dataclass.
        """
        metadata = _safe_json_loads(model.agent_metadata, "agent_metadata", model.agent_id)
        manifest_list = _safe_json_loads(model.context_manifest, "context_manifest", model.agent_id)

        return AgentRecord(
            agent_id=model.agent_id,
            owner_id=model.owner_id,
            zone_id=model.zone_id,
            name=model.name,
            state=AgentState(model.state),
            generation=model.generation,
            last_heartbeat=model.last_heartbeat,
            metadata=types.MappingProxyType(metadata),
            created_at=model.created_at,
            updated_at=model.updated_at,
            context_manifest=tuple(manifest_list),
        )

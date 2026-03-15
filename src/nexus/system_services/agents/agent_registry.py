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

import asyncio
import json
import logging
import types
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from nexus.contracts.agent_types import (
    AgentRecord,
    AgentResources,
    AgentResourceUsage,
    AgentSpec,
    AgentState,
    AgentStatus,
    QoSClass,
    derive_phase,
    is_new_session,
    validate_transition,
)
from nexus.contracts.protocols.agent_registry import AgentInfo
from nexus.contracts.qos import EVICTION_ORDER, AgentQoS
from nexus.storage.models import AgentRecordModel
from nexus.system_services.agents.heartbeat_buffer import HeartbeatBuffer

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.contracts.agent_types import AgentCondition
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


def _safe_json_loads(raw: str | None, field_name: str, agent_id: str) -> Any:
    """Safely deserialize a JSON text column, returning a typed default on failure.

    Returns {} for 'agent_metadata', [] for all other fields.
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


class AgentRegistry:
    """Agent registry with lifecycle state machine and session generation counters.

    Federation-safe: no process-local caches or locks.
    All reads go directly to the database.

    Heartbeat buffering is delegated to :class:`HeartbeatBuffer` (Issue #1589).
    Database operations use session-per-operation pattern (no held sessions).

    Args:
        record_store: RecordStoreABC providing database access.
        entity_registry: Optional EntityRegistry for backward compatibility bridge.
        flush_interval: Seconds between heartbeat buffer flushes (default: 60).
        max_buffer_size: Hard cap on heartbeat buffer entries (default: 50_000).
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        entity_registry: Any = None,
        flush_interval: int = 60,
        max_buffer_size: int = 50_000,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._entity_registry = entity_registry
        # Heartbeat buffer composed via DI (Issue #1589)
        self._heartbeat_buffer = HeartbeatBuffer(
            flush_callback=self._flush_to_db,
            flush_interval=flush_interval,
            max_buffer_size=max_buffer_size,
        )

    @contextmanager
    def _get_session(self) -> "Generator[Session, None, None]":
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
        qos: AgentQoS | None = None,
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
            qos: Optional QoS assignment (Issue #2171). Defaults to
                standard scheduling and eviction class.

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

        # Store QoS in metadata (Issue #2171)
        agent_qos = qos if qos is not None else AgentQoS()
        metadata = dict(metadata) if metadata else {}
        metadata["qos"] = agent_qos.to_dict()

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
                agent_metadata=json.dumps(metadata),
                eviction_priority=EVICTION_ORDER.get(agent_qos.eviction_class, 1),
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

        Args:
            agent_id: Agent identifier.

        Returns:
            AgentRecord if found, None otherwise.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                return None

            return self._model_to_record(model)

    def transition(
        self,
        agent_id: str,
        target_state: AgentState | str,
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
            target_state: Desired target state (AgentState enum or string value).
            expected_generation: Expected generation for optimistic locking.
                If None, locking check is skipped (state CAS still applies).

        Returns:
            New AgentRecord snapshot after transition.

        Raises:
            ValueError: If agent not found.
            InvalidTransitionError: If transition is not allowed.
            StaleAgentError: If expected_generation doesn't match (concurrent modification).
        """
        if isinstance(target_state, str):
            target_state = AgentState(target_state)

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

            # Deserialize QoS from metadata (Issue #2171)
            qos_data = metadata_dict.get("qos")
            agent_qos = AgentQoS.from_dict(qos_data) if isinstance(qos_data, dict) else AgentQoS()

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
                qos=agent_qos,
            )

        logger.debug(
            "[AGENT-REG] Transition %s: %s -> %s (gen %d -> %d)",
            agent_id,
            current_state.value,
            target_state.value,
            model.generation,
            new_generation,
        )

        return record

    def heartbeat(self, agent_id: str) -> None:
        """Record a heartbeat for an agent.

        Writes to the composed HeartbeatBuffer (Decision #13A). The buffer
        is flushed to DB when the flush_interval elapses. This reduces write
        amplification from frequent heartbeats.

        No existence check — the buffer is flushed via UPDATE which silently
        skips non-existent agents (0 rows affected). This avoids a DB read
        on every heartbeat call (Issue #2170, fix 6A).

        Args:
            agent_id: Agent identifier.
        """
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

        # Remove from heartbeat buffer
        self._heartbeat_buffer.remove(agent_id)

        logger.debug("[AGENT-REG] Unregistered agent %s", agent_id)
        return True

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

    def list_eviction_candidates(self, batch_size: int = 10) -> list[AgentRecord]:
        """Return CONNECTED agents ordered for eviction, limited to batch_size.

        Orders by eviction_priority ASC (spot=0 first, premium=2 last), then
        by last_heartbeat ASC NULLS FIRST within each QoS tier (Issue #2171).

        Excludes agents with recent buffer heartbeats via SQL NOT IN clause
        and applies SQL LIMIT to avoid O(N) ORM hydration.

        Args:
            batch_size: Maximum number of candidates to return.

        Returns:
            List of AgentRecord snapshots ordered for eviction.
        """
        # Get all buffered agent IDs (any buffered heartbeat = recently active)
        buffered_ids = self._heartbeat_buffer.recently_heartbeated(datetime(1970, 1, 1, tzinfo=UTC))

        with self._get_session() as session:
            stmt = (
                select(AgentRecordModel)
                .where(AgentRecordModel.state == AgentState.CONNECTED.value)
                .order_by(
                    AgentRecordModel.eviction_priority.asc(),
                    AgentRecordModel.last_heartbeat.asc().nullsfirst(),
                )
            )
            # Push buffer exclusion into SQL to avoid hydrating excluded rows
            if buffered_ids:
                stmt = stmt.where(AgentRecordModel.agent_id.not_in(list(buffered_ids)))
            stmt = stmt.limit(batch_size)

            models = list(session.execute(stmt).scalars().all())
            records = [self._model_to_record(m) for m in models]

        return records

    def count_connected_agents(self) -> int:
        """Return the count of CONNECTED agents.

        Lightweight SELECT COUNT(*) for agent cap checks — avoids
        hydrating full ORM models.

        Returns:
            Number of agents in CONNECTED state.
        """
        with self._get_session() as session:
            stmt = (
                select(sa.func.count())
                .select_from(AgentRecordModel)
                .where(AgentRecordModel.state == AgentState.CONNECTED.value)
            )
            result: int = session.execute(stmt).scalar_one()
            return result

    def checkpoint(self, agent_id: str, checkpoint_data: dict[str, Any]) -> None:
        """Save checkpoint data to agent_metadata['_nexus_checkpoint'] before eviction.

        Stores checkpoint in the existing agent_metadata JSON column under
        the '_nexus_checkpoint' key to avoid collision with user metadata.

        Args:
            agent_id: Agent identifier.
            checkpoint_data: Arbitrary checkpoint data to preserve.

        Raises:
            ValueError: If agent not found.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                raise ValueError(f"Agent '{agent_id}' not found")

            metadata = _safe_json_loads(model.agent_metadata, "agent_metadata", agent_id)
            metadata["_nexus_checkpoint"] = checkpoint_data

            now = datetime.now(UTC)
            stmt = (
                update(AgentRecordModel)
                .where(AgentRecordModel.agent_id == agent_id)
                .values(agent_metadata=json.dumps(metadata), updated_at=now)
            )
            session.execute(stmt)
            session.flush()

    def restore_checkpoint(self, agent_id: str) -> dict[str, Any] | None:
        """Load and clear checkpoint data on reactivation.

        Returns the saved checkpoint data and removes it from metadata to
        avoid stale data on subsequent restores.

        Args:
            agent_id: Agent identifier.

        Returns:
            Checkpoint data dict, or None if no checkpoint exists.

        Raises:
            ValueError: If agent not found.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                raise ValueError(f"Agent '{agent_id}' not found")

            metadata = _safe_json_loads(model.agent_metadata, "agent_metadata", agent_id)
            checkpoint_data = metadata.pop("_nexus_checkpoint", None)

            if checkpoint_data is not None:
                now = datetime.now(UTC)
                stmt = (
                    update(AgentRecordModel)
                    .where(AgentRecordModel.agent_id == agent_id)
                    .values(agent_metadata=json.dumps(metadata), updated_at=now)
                )
                session.execute(stmt)
                session.flush()

            return checkpoint_data  # type: ignore[no-any-return]

    def batch_checkpoint(self, checkpoints: dict[str, dict[str, Any]]) -> int:
        """Batch-write checkpoints for multiple agents.

        Uses a single SELECT ... WHERE agent_id IN (...) to fetch all models,
        then individual UPDATEs within the same session (avoids N+1 SELECTs).

        Note: We use N individual UPDATEs (not a single bulk UPDATE) because
        each agent's metadata JSON must be merged individually. A single
        executemany would require identical SET shapes, but each agent may have
        different existing metadata. The single-session approach keeps this
        within one transaction and one DB round-trip for the SELECT.

        Args:
            checkpoints: Mapping of agent_id -> checkpoint_data.

        Returns:
            Number of checkpoints successfully written.
        """
        if not checkpoints:
            return 0

        agent_ids = list(checkpoints.keys())
        written = 0
        with self._get_session() as session:
            # Single batch fetch instead of N individual SELECTs
            stmt = select(AgentRecordModel).where(AgentRecordModel.agent_id.in_(agent_ids))
            models = {m.agent_id: m for m in session.execute(stmt).scalars().all()}

            now = datetime.now(UTC)
            for agent_id, checkpoint_data in checkpoints.items():
                model = models.get(agent_id)
                if model is None:
                    continue

                metadata = _safe_json_loads(model.agent_metadata, "agent_metadata", agent_id)
                metadata["_nexus_checkpoint"] = checkpoint_data

                upd = (
                    update(AgentRecordModel)
                    .where(AgentRecordModel.agent_id == agent_id)
                    .values(agent_metadata=json.dumps(metadata), updated_at=now)
                )
                session.execute(upd)
                written += 1

            session.flush()

        return written

    def cleanup_stale_checkpoints(self, max_age_seconds: int = 86400) -> int:
        """Remove stale checkpoint data from SUSPENDED agents.

        Scans SUSPENDED agents with _nexus_checkpoint data and removes
        checkpoints older than max_age_seconds. This prevents unbounded
        growth of checkpoint data for agents that never reconnect.

        Args:
            max_age_seconds: Maximum checkpoint age in seconds before removal.

        Returns:
            Number of stale checkpoints cleaned up.
        """
        import time

        cleaned = 0
        cutoff = time.time() - max_age_seconds

        with self._get_session() as session:
            stmt = select(AgentRecordModel).where(
                AgentRecordModel.state == AgentState.SUSPENDED.value,
            )
            models = list(session.execute(stmt).scalars().all())

            now = datetime.now(UTC)
            for model in models:
                metadata = _safe_json_loads(model.agent_metadata, "agent_metadata", model.agent_id)
                checkpoint = metadata.get("_nexus_checkpoint")
                if checkpoint is None:
                    continue

                evicted_at = checkpoint.get("evicted_at", 0)
                if evicted_at < cutoff:
                    del metadata["_nexus_checkpoint"]
                    upd = (
                        update(AgentRecordModel)
                        .where(AgentRecordModel.agent_id == model.agent_id)
                        .values(agent_metadata=json.dumps(metadata), updated_at=now)
                    )
                    session.execute(upd)
                    cleaned += 1

            session.flush()

        if cleaned > 0:
            logger.info(
                "[AGENT-REG] Cleaned up %d stale checkpoints (max_age=%ds)",
                cleaned,
                max_age_seconds,
            )
        return cleaned

    # ------------------------------------------------------------------
    # Spec / Status methods (Issue #2169)
    # ------------------------------------------------------------------

    def set_spec(self, agent_id: str, spec: AgentSpec) -> AgentSpec:
        """Store an AgentSpec for an agent, serialized as JSON.

        Increments the spec_generation on each call to enable drift detection.
        Returns the stored spec with updated generation (no re-read needed).

        Performance: 2 queries total (SELECT for existence + generation, UPDATE).

        Args:
            agent_id: Agent identifier.
            spec: Desired state specification.

        Returns:
            The stored AgentSpec with updated spec_generation.

        Raises:
            ValueError: If agent not found.
        """
        spec_dict = self._spec_to_dict(spec)

        with self._get_session() as session:
            row = session.execute(
                select(AgentRecordModel.agent_id, AgentRecordModel.agent_spec).where(
                    AgentRecordModel.agent_id == agent_id
                )
            ).one_or_none()

            if row is None:
                raise ValueError(f"Agent '{agent_id}' not found")

            # Load existing spec to determine next generation
            existing_spec = self._parse_spec_json(row.agent_spec, agent_id)
            next_gen = (existing_spec.spec_generation + 1) if existing_spec else 1
            spec_dict["spec_generation"] = next_gen

            now = datetime.now(UTC)
            spec_json = json.dumps(spec_dict)
            stmt = (
                update(AgentRecordModel)
                .where(AgentRecordModel.agent_id == agent_id)
                .values(agent_spec=spec_json, updated_at=now)
            )
            session.execute(stmt)
            session.flush()

        logger.debug("[AGENT-REG] Set spec for agent %s (gen %d)", agent_id, next_gen)
        # Return the spec from known values — no re-read needed
        stored = self._parse_spec_json(spec_json, agent_id)
        assert stored is not None  # noqa: S101 — we just wrote valid JSON
        return stored

    def get_spec(self, agent_id: str) -> AgentSpec | None:
        """Retrieve the stored AgentSpec for an agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            AgentSpec if stored, None if agent has no spec or doesn't exist.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                return None

            return self._parse_spec_json(model.agent_spec, agent_id)

    def get_status(self, agent_id: str) -> AgentStatus | None:
        """Compute the current AgentStatus for an agent.

        Status is derived on read from the agent record, heartbeat buffer,
        and stored spec. Single DB query + in-memory computation.

        Args:
            agent_id: Agent identifier.

        Returns:
            Computed AgentStatus, or None if agent doesn't exist.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentRecordModel).where(AgentRecordModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                return None

            state = AgentState(model.state)
            spec = self._parse_spec_json(model.agent_spec, agent_id)

            # Determine observed_generation from spec
            observed_gen = spec.spec_generation if spec else 0

            # Check heartbeat buffer for latest heartbeat
            last_hb = self._heartbeat_buffer.get_latest(agent_id)
            if last_hb is None:
                last_hb = model.last_heartbeat

            # Build conditions (empty for now — future extensions add conditions)
            conditions: tuple[AgentCondition, ...] = ()

            phase = derive_phase(state, conditions)

            return AgentStatus(
                phase=phase,
                observed_generation=observed_gen,
                conditions=conditions,
                resource_usage=AgentResourceUsage(),
                last_heartbeat=last_hb,
                last_activity=model.updated_at,
                inbox_depth=0,
                context_usage_pct=0.0,
            )

    @staticmethod
    def _spec_to_dict(spec: AgentSpec) -> dict[str, Any]:
        """Serialize an AgentSpec to a JSON-safe dict."""
        return {
            "agent_type": spec.agent_type,
            "capabilities": sorted(spec.capabilities),
            "resource_requests": {
                "token_budget": spec.resource_requests.token_budget,
                "token_request": spec.resource_requests.token_request,
                "storage_limit_mb": spec.resource_requests.storage_limit_mb,
                "context_limit": spec.resource_requests.context_limit,
            },
            "resource_limits": {
                "token_budget": spec.resource_limits.token_budget,
                "token_request": spec.resource_limits.token_request,
                "storage_limit_mb": spec.resource_limits.storage_limit_mb,
                "context_limit": spec.resource_limits.context_limit,
            },
            "qos_class": str(spec.qos_class),
            "zone_affinity": spec.zone_affinity,
            "spec_generation": spec.spec_generation,
        }

    @staticmethod
    def _parse_spec_json(raw: str | None, agent_id: str) -> AgentSpec | None:
        """Deserialize an AgentSpec from a JSON text column."""
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return AgentSpec(
                agent_type=data.get("agent_type", ""),
                capabilities=frozenset(data.get("capabilities", [])),
                resource_requests=AgentResources(**data.get("resource_requests", {})),
                resource_limits=AgentResources(**data.get("resource_limits", {})),
                qos_class=QoSClass(data.get("qos_class", "standard")),
                zone_affinity=data.get("zone_affinity"),
                spec_generation=data.get("spec_generation", 1),
            )
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            logger.warning("[AGENT-REG] Corrupt agent_spec for agent %s", agent_id)
            return None

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

        # Deserialize QoS from metadata (Issue #2171)
        qos_data = metadata.get("qos")
        agent_qos = AgentQoS.from_dict(qos_data) if isinstance(qos_data, dict) else AgentQoS()

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
            qos=agent_qos,
        )


# ── Async wrapper (merged from async_agent_registry.py, Issue #1440) ──


def _to_agent_info(record: "AgentRecord") -> AgentInfo:
    """Convert an ``AgentRecord`` to the protocol-level ``AgentInfo``."""
    return AgentInfo(
        agent_id=record.agent_id,
        owner_id=record.owner_id,
        zone_id=record.zone_id,
        name=record.name,
        state=record.state.value,
        generation=record.generation,
    )


class AsyncAgentRegistry:
    """Async adapter for ``AgentRegistry`` conforming to ``AgentRegistryProtocol``.

    All methods with DB I/O delegate via ``asyncio.to_thread``.
    The ``AgentRecord`` → ``AgentInfo`` conversion happens at the boundary.
    """

    def __init__(
        self,
        inner: "AgentRegistry",
        *,
        state_emitter: Any | None = None,
    ) -> None:
        self._inner = inner
        self._state_emitter = state_emitter

    async def register(
        self,
        agent_id: str,
        owner_id: str,
        *,
        zone_id: str | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInfo:
        record = await asyncio.to_thread(
            self._inner.register,
            agent_id,
            owner_id,
            zone_id=zone_id,
            name=name,
            metadata=metadata,
        )
        return _to_agent_info(record)

    async def get(self, agent_id: str) -> AgentInfo | None:
        record = await asyncio.to_thread(self._inner.get, agent_id)
        if record is None:
            return None
        return _to_agent_info(record)

    async def transition(
        self,
        agent_id: str,
        target_state: str,
        *,
        expected_generation: int | None = None,
    ) -> AgentInfo:
        try:
            state_enum = AgentState(target_state)
        except ValueError:
            valid = [s.value for s in AgentState]
            raise ValueError(
                f"Invalid target state {target_state!r}. Valid: {', '.join(valid)}"
            ) from None

        previous_state: str | None = None
        if self._state_emitter is not None:
            before_record = await asyncio.to_thread(self._inner.get, agent_id)
            if before_record is not None:
                previous_state = before_record.state.value

        record = await asyncio.to_thread(
            self._inner.transition,
            agent_id,
            state_enum,
            expected_generation=expected_generation,
        )
        info = _to_agent_info(record)

        if self._state_emitter is not None and previous_state is not None:
            from nexus.system_services.scheduler.events import AgentStateEvent

            event = AgentStateEvent(
                agent_id=agent_id,
                previous_state=previous_state,
                new_state=info.state,
                generation=info.generation,
                zone_id=info.zone_id,
            )
            await self._state_emitter.emit(event)

        return info

    async def heartbeat(self, agent_id: str) -> None:
        await asyncio.to_thread(self._inner.heartbeat, agent_id)

    async def list_by_zone(self, zone_id: str) -> list[AgentInfo]:
        records = await asyncio.to_thread(self._inner.list_by_zone, zone_id)
        return [_to_agent_info(r) for r in records]

    async def unregister(self, agent_id: str) -> bool:
        return await asyncio.to_thread(self._inner.unregister, agent_id)

    async def set_spec(self, agent_id: str, spec: "AgentSpec") -> "AgentSpec":
        return await asyncio.to_thread(self._inner.set_spec, agent_id, spec)

    async def get_spec(self, agent_id: str) -> "AgentSpec | None":
        return await asyncio.to_thread(self._inner.get_spec, agent_id)

    async def get_status(self, agent_id: str) -> "AgentStatus | None":
        return await asyncio.to_thread(self._inner.get_status, agent_id)

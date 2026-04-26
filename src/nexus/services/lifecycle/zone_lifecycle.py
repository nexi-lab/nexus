"""Zone lifecycle service — ordered zone deprovisioning (Issue #2061).

Orchestrates zone teardown using a Kubernetes-inspired finalizer pattern:

1. Zone enters ``Terminating`` phase → writes gated, reads allowed.
2. Registered finalizers run in dependency order (concurrent, then sequential).
3. Finalizers removed as they complete; zone enters ``Terminated`` when empty.
4. Per-finalizer timeout (30 s) + reconciler retry on failure.

Reuses patterns: state machine with per-entry locks, exponential backoff with jitter.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from nexus.contracts.protocols.zone_lifecycle import (
    ZoneDeprovisionResult,
    ZoneFinalizerProtocol,
    ZonePhase,
)

logger = logging.getLogger(__name__)

# Per-finalizer timeout (Decision #16A)
_FINALIZER_TIMEOUT_S: float = 30.0

# Finalizer execution phases
FinalizerPhase = Literal["concurrent", "sequential"]


class ZoneLifecycleService:
    """Orchestrates ordered zone deprovisioning with finalizer cleanup.

    Args:
        session_factory: Callable returning a SQLAlchemy session.
    """

    def __init__(self, session_factory: Callable[..., Any]) -> None:
        self._session_factory = session_factory
        self._finalizers: list[tuple[ZoneFinalizerProtocol, FinalizerPhase]] = []
        # In-memory set for O(1) write-gating checks (Decision #14A)
        self._terminating_zones: set[str] = set()
        # Per-zone lock to prevent concurrent deprovision races
        self._zone_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_finalizer(
        self,
        finalizer: ZoneFinalizerProtocol,
        *,
        phase: FinalizerPhase = "concurrent",
    ) -> None:
        """Register a finalizer to run during zone deprovisioning.

        Args:
            finalizer: Service implementing ``ZoneFinalizerProtocol``.
            phase: Execution phase — ``"concurrent"`` (default) runs in
                parallel with other concurrent finalizers; ``"sequential"``
                runs after all concurrent finalizers complete (e.g. ReBAC
                must run last because other finalizers may need permissions).
        """
        if not isinstance(finalizer, ZoneFinalizerProtocol):
            raise TypeError(f"Expected ZoneFinalizerProtocol, got {type(finalizer).__name__}")
        self._finalizers.append((finalizer, phase))
        logger.debug(
            "[ZoneLifecycle] Registered finalizer: %s (phase=%s)",
            finalizer.finalizer_key,
            phase,
        )

    # ------------------------------------------------------------------
    # BackgroundService lifecycle (auto-managed by ServiceRegistry)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load terminating zones from DB at bootstrap.

        Called by ServiceRegistry.start_background_services(). Replaces
        the old _bootstrap_callbacks pattern.
        """
        try:
            if self._session_factory is not None:
                with self._session_factory() as session:
                    self.load_terminating_zones(session)
                logger.debug(
                    "[ZoneLifecycle] Loaded terminating zones at BackgroundService.start()"
                )
        except Exception as exc:
            logger.warning("[ZoneLifecycle] Failed to load terminating zones: %s", exc)

    async def stop(self) -> None:
        """No-op — in-memory set, no cleanup needed."""

    # ------------------------------------------------------------------
    # Write-gating (Decision #4A / #14A)
    # ------------------------------------------------------------------

    def is_zone_terminating(self, zone_id: str) -> bool:
        """O(1) check whether *zone_id* is in Terminating phase."""
        return zone_id in self._terminating_zones

    def load_terminating_zones(self, session: Any) -> None:
        """Load Terminating zones from DB on startup (sync — pure SQLAlchemy).

        Must only be called at startup, before any ``deprovision_zone`` calls.
        """
        from sqlalchemy import select

        from nexus.storage.models import ZoneModel

        stmt = select(ZoneModel.zone_id).where(ZoneModel.phase == "Terminating")
        rows = session.scalars(stmt).all()
        self._terminating_zones = set(rows)
        if self._terminating_zones:
            logger.info(
                "[ZoneLifecycle] Loaded %d terminating zones from DB",
                len(self._terminating_zones),
            )

    # ------------------------------------------------------------------
    # Deprovision orchestration
    # ------------------------------------------------------------------

    async def deprovision_zone(self, zone_id: str, session: Any) -> ZoneDeprovisionResult:
        """Begin or continue zone deprovisioning.

        - If Active → sets phase to Terminating, runs finalizers.
        - If already Terminating → retries pending finalizers.
        - If Terminated → returns immediately.

        Returns:
            ZoneDeprovisionResult with completed/pending/failed finalizers.

        Raises:
            ValueError: If ``zone_id`` is the reserved ROOT_ZONE_ID.
                The default zone is required by ``api_key_zones`` FK and
                by the bootstrap invariant in
                ``nexus.storage.zone_bootstrap.ensure_root_zone`` (#3897);
                deprovisioning it would block server startup and break
                root-scoped key creation.
        """
        from datetime import UTC, datetime

        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.storage.models import ZoneModel

        if zone_id == ROOT_ZONE_ID:
            raise ValueError(
                f"Refusing to deprovision reserved zone {ROOT_ZONE_ID!r}: "
                "default zone is required by api_key_zones FK and "
                "server startup (Issue #3897)."
            )

        # Per-zone lock to prevent concurrent races (Decision #11A)
        lock = self._zone_locks.setdefault(zone_id, asyncio.Lock())

        async with lock:
            zone = session.get(ZoneModel, zone_id)
            if zone is None:
                return ZoneDeprovisionResult(
                    zone_id=zone_id,
                    phase=ZonePhase.TERMINATED,
                    finalizers_completed=(),
                    finalizers_pending=(),
                    finalizers_failed={"_": f"Zone '{zone_id}' not found"},
                )

            if zone.phase == ZonePhase.TERMINATED:
                return ZoneDeprovisionResult(
                    zone_id=zone_id,
                    phase=ZonePhase.TERMINATED,
                    finalizers_completed=(),
                    finalizers_pending=(),
                    finalizers_failed={},
                )

            # Transition to Terminating if Active
            if zone.phase == ZonePhase.ACTIVE:
                finalizer_keys = [f.finalizer_key for f, _phase in self._finalizers]
                zone.phase = ZonePhase.TERMINATING
                zone.finalizers = json.dumps(finalizer_keys)
                # Commit first, then update in-memory state.  If commit fails,
                # the in-memory set stays consistent with the database.
                session.commit()
                self._terminating_zones.add(zone_id)
                logger.info(
                    "[ZoneLifecycle] Zone %s → Terminating (finalizers=%s)",
                    zone_id,
                    finalizer_keys,
                )

            # Run finalizers
            pending_keys = zone.parsed_finalizers
            result = await self._run_finalizers(zone_id, pending_keys)

            # Update DB state
            remaining = list(result.finalizers_pending)
            if not remaining and not result.finalizers_failed:
                zone.phase = ZonePhase.TERMINATED
                zone.finalizers = "[]"
                zone.deleted_at = datetime.now(UTC)
                self._terminating_zones.discard(zone_id)
                self._zone_locks.pop(zone_id, None)
                logger.info("[ZoneLifecycle] Zone %s → Terminated", zone_id)
            else:
                zone.finalizers = json.dumps(remaining)

            session.commit()
            return result

    async def _run_finalizers(self, zone_id: str, pending_keys: list[str]) -> ZoneDeprovisionResult:
        """Execute finalizers in two phases: concurrent then sequential.

        Decision #13A / #4B: Finalizers registered with ``phase="concurrent"``
        run in parallel first; those with ``phase="sequential"`` run one-by-one
        afterward.  This ensures resources like ReBAC (sequential) remain
        available while other finalizers clean up.
        """
        # Build lookup: key → finalizer
        by_key: dict[str, ZoneFinalizerProtocol] = {
            f.finalizer_key: f for f, _ph in self._finalizers
        }
        phase_of: dict[str, FinalizerPhase] = {f.finalizer_key: ph for f, ph in self._finalizers}

        # Partition pending keys into concurrent and sequential
        concurrent_keys = [
            k for k in pending_keys if k in by_key and phase_of.get(k) == "concurrent"
        ]
        sequential_keys = [
            k for k in pending_keys if k in by_key and phase_of.get(k) == "sequential"
        ]

        completed: list[str] = []
        failed: dict[str, str] = {}
        still_pending: list[str] = []

        # Phase 1: Concurrent finalizers
        if concurrent_keys:
            results = await self._run_concurrent(zone_id, concurrent_keys, by_key)
            for key in concurrent_keys:
                if key in results:
                    failed[key] = results[key]
                    still_pending.append(key)
                else:
                    completed.append(key)

        # Phase 2: Sequential finalizers (e.g. ReBAC last)
        for key in sequential_keys:
            error = await self._run_single(zone_id, key, by_key[key])
            if error:
                failed[key] = error
                still_pending.append(key)
            else:
                completed.append(key)

        # Keys not in our registry (orphaned) stay pending
        known_keys = set(by_key)
        for key in pending_keys:
            if key not in known_keys:
                still_pending.append(key)

        return ZoneDeprovisionResult(
            zone_id=zone_id,
            phase=ZonePhase.TERMINATING if still_pending else ZonePhase.TERMINATED,
            finalizers_completed=tuple(completed),
            finalizers_pending=tuple(still_pending),
            finalizers_failed=failed,
        )

    async def _run_concurrent(
        self,
        zone_id: str,
        keys: list[str],
        by_key: dict[str, ZoneFinalizerProtocol],
    ) -> dict[str, str]:
        """Run multiple finalizers concurrently. Returns key → error for failures."""
        errors: dict[str, str] = {}

        async def _run(key: str) -> None:
            err = await self._run_single(zone_id, key, by_key[key])
            if err:
                errors[key] = err

        async with asyncio.TaskGroup() as tg:
            for key in keys:
                tg.create_task(_run(key))

        return errors

    async def _run_single(
        self, zone_id: str, key: str, finalizer: ZoneFinalizerProtocol
    ) -> str | None:
        """Run a single finalizer with timeout. Returns error message or None."""
        try:
            await asyncio.wait_for(
                finalizer.finalize_zone(zone_id),
                timeout=_FINALIZER_TIMEOUT_S,
            )
            logger.info("[ZoneLifecycle] Finalizer %s completed for zone %s", key, zone_id)
            return None
        except TimeoutError:
            msg = f"Finalizer {key} timed out after {_FINALIZER_TIMEOUT_S}s"
            logger.warning("[ZoneLifecycle] %s (zone=%s)", msg, zone_id)
            return msg
        except Exception as exc:
            msg = f"Finalizer {key} failed: {exc}"
            logger.warning("[ZoneLifecycle] %s (zone=%s)", msg, zone_id)
            return msg

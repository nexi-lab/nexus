"""Post-flush lineage hook — records agent lineage on write (Issue #3417).

Creates a post-flush hook function that reads accumulated session reads
and persists them as a lineage aspect + reverse index entries on the
written file's entity.

The hook:
    1. Iterates flushed events, filtering for "write" and "copy" ops
    2. Agent-gates: only processes events with agent_id set
    3. Consumes reads from the SessionReadAccumulator
    4. Builds LineageAspect and calls LineageService.record_lineage()
    5. Wraps each file's lineage in a savepoint (atomic per file)

Failures are logged and ignored — lineage is best-effort.
Recovery: ``nexus reindex --target lineage`` catches gaps.
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def make_lineage_hook(
    *,
    session_factory: Callable[..., Any],
) -> Callable[[list[dict[str, Any]]], None]:
    """Create a post-flush hook for agent lineage recording.

    Args:
        session_factory: RecordStore session factory for LineageService.

    Returns:
        Hook function: ``(events: list[dict]) -> None``
    """

    def lineage_hook(events: list[dict[str, Any]]) -> None:
        """Record lineage for agent writes (best-effort, runs in background)."""
        # Filter to agent write/copy events only
        agent_events = [e for e in events if e.get("agent_id") and e.get("op") in ("write", "copy")]
        if not agent_events:
            return

        # Run in background thread to avoid blocking the post-flush pipeline
        thread = threading.Thread(
            target=_record_lineage_batch,
            args=(agent_events, session_factory),
            daemon=True,
        )
        thread.start()

    return lineage_hook


def _record_lineage_batch(
    events: list[dict[str, Any]],
    session_factory: Callable[..., Any],
) -> None:
    """Record lineage for a batch of agent events (runs in background thread)."""
    try:
        from nexus.contracts.aspects import LineageAspect
        from nexus.contracts.urn import NexusURN
        from nexus.storage.lineage_service import LineageService
        from nexus.storage.session_read_accumulator import get_accumulator

        accumulator = get_accumulator()

        with session_factory() as session:
            lineage_service = LineageService(session)
            recorded = 0

            for event in events:
                try:
                    agent_id = event.get("agent_id", "")
                    if not agent_id:
                        continue

                    path = event.get("path", "")
                    zone_id = event.get("zone_id")
                    metadata = event.get("metadata", {})
                    op = event.get("op", "write")

                    urn = str(NexusURN.for_file(zone_id or "default", path))

                    if op == "copy":
                        # Copy lineage: source file is the single upstream
                        src_path = event.get("src_path", "")
                        if not src_path:
                            continue
                        src_metadata = event.get("src_metadata", {})
                        lineage = LineageAspect.from_explicit_declaration(
                            upstream=[
                                {
                                    "path": src_path,
                                    "version": src_metadata.get("version", 0),
                                    "content_id": src_metadata.get("content_id", ""),
                                }
                            ],
                            agent_id=agent_id,
                            agent_generation=event.get("agent_generation"),
                        )
                        lineage.operation = "copy"
                    else:
                        # Write lineage: consume accumulated reads from active scope.
                        # If the agent used begin_scope(), only that scope's reads
                        # are consumed. Otherwise the default scope is used.
                        agent_generation = event.get("agent_generation")
                        scope_id = event.get("lineage_scope")  # explicit scope from event
                        reads = accumulator.consume(agent_id, agent_generation, scope_id=scope_id)

                        if not reads:
                            # No accumulated reads — nothing to record
                            continue

                        duration_ms = metadata.get("duration_ms")
                        lineage = LineageAspect.from_session_reads(
                            reads=reads,
                            agent_id=agent_id,
                            agent_generation=agent_generation,
                            operation=op,
                            duration_ms=duration_ms,
                        )

                    # Savepoint: one file's failure doesn't affect others
                    with session.begin_nested():
                        lineage_service.record_lineage(
                            entity_urn=urn,
                            lineage=lineage,
                            zone_id=zone_id,
                            downstream_path=path,
                        )
                        recorded += 1

                except Exception:
                    logger.debug(
                        "Lineage recording failed for %s (non-critical)",
                        event.get("path"),
                        exc_info=True,
                    )

            if recorded > 0:
                session.commit()
                logger.debug(
                    "Post-flush lineage: %d/%d agent events recorded",
                    recorded,
                    len(events),
                )

    except Exception:
        logger.debug(
            "Post-flush lineage batch failed (non-critical)",
            exc_info=True,
        )

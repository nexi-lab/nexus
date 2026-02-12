"""Background Merkle checkpoint task for exchange audit log.

Issue #1360 Phase 1: Periodically computes Merkle roots over ranges
of audit records. Runs as an asyncio background task registered in
FastAPI lifespan.

Scheduling (Decision #15): every 5 minutes OR when 1000 uncovered
records accumulate, whichever comes first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexus.storage.exchange_audit_logger import ExchangeAuditLogger, _build_merkle_root
from nexus.storage.models.audit_checkpoint import AuditCheckpointModel
from nexus.storage.models.exchange_audit_log import ExchangeAuditLogModel

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes
DEFAULT_THRESHOLD = 1000


class MerkleCheckpointTask:
    """Background task that computes periodic Merkle root checkpoints."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        audit_logger: ExchangeAuditLogger,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        threshold: int = DEFAULT_THRESHOLD,
    ) -> None:
        self._session_factory = session_factory
        self._audit_logger = audit_logger
        self._interval = interval_seconds
        self._threshold = threshold
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background checkpoint loop."""
        self._task = asyncio.create_task(self._run_loop())
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "Merkle checkpoint task started (interval=%ds, threshold=%d)",
                self._interval,
                self._threshold,
            )

    async def stop(self) -> None:
        """Stop the background checkpoint loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("Merkle checkpoint task stopped")

    async def _run_loop(self) -> None:
        """Main loop: sleep → check → maybe checkpoint."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await asyncio.to_thread(self._maybe_checkpoint)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Merkle checkpoint error", exc_info=True)

    def _maybe_checkpoint(self) -> str | None:
        """Check for uncovered records and create a checkpoint if needed.

        Returns:
            Checkpoint ID if created, None otherwise.
        """
        session = self._session_factory()
        try:
            # Find the last checkpoint
            last_cp = session.execute(
                select(AuditCheckpointModel)
                .order_by(AuditCheckpointModel.checkpoint_at.desc())
                .limit(1)
            ).scalar_one_or_none()

            # Count uncovered records
            uncovered_stmt = select(func.count()).select_from(ExchangeAuditLogModel)
            if last_cp is not None:
                uncovered_stmt = uncovered_stmt.where(
                    ExchangeAuditLogModel.created_at > last_cp.checkpoint_at
                )
            uncovered_count: int = session.execute(uncovered_stmt).scalar_one()

            if uncovered_count < self._threshold:
                return None

            # Get the range of uncovered records (capped to threshold)
            range_stmt = (
                select(
                    ExchangeAuditLogModel.id,
                    ExchangeAuditLogModel.created_at,
                    ExchangeAuditLogModel.record_hash,
                )
                .order_by(
                    ExchangeAuditLogModel.created_at,
                    ExchangeAuditLogModel.id,
                )
                .limit(self._threshold)
            )
            if last_cp is not None:
                range_stmt = range_stmt.where(
                    ExchangeAuditLogModel.created_at > last_cp.checkpoint_at
                )
            rows = list(session.execute(range_stmt))

            if not rows:
                return None

            first_id = rows[0][0]
            last_id = rows[-1][0]
            last_ts = rows[-1][1]
            hashes = [row[2] for row in rows]

            merkle_root = _build_merkle_root(hashes)

            checkpoint = AuditCheckpointModel(
                checkpoint_at=last_ts,
                record_count=len(rows),
                merkle_root=merkle_root,
                first_record_id=first_id,
                last_record_id=last_id,
                created_at=datetime.now(UTC),
            )
            session.add(checkpoint)
            session.commit()

            checkpoint_id: str = checkpoint.id

            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "Merkle checkpoint created: %s (%d records, root=%s)",
                    checkpoint_id,
                    len(rows),
                    merkle_root[:12],
                )
            return checkpoint_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

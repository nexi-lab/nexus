"""Shared lifecycle helpers for IPC message management.

Provides ``dead_letter_message()`` — a single implementation used by both
``MessageProcessor`` and ``TTLSweeper`` for consistent dead-lettering with
structured ``.reason.json`` sidecars.

Issue #3197: extracted to eliminate DRY violation between delivery.py and sweep.py.
"""

import json
import logging
from datetime import datetime
from typing import Any

from nexus.bricks.ipc.conventions import dead_letter_path, message_path_in_dead_letter
from nexus.bricks.ipc.exceptions import DLQReason

logger = logging.getLogger(__name__)


async def dead_letter_message(
    vfs: Any,
    msg_path: str,
    agent_id: str,
    zone_id: str,
    reason: DLQReason,
    *,
    msg_id: str | None = None,
    timestamp: datetime | None = None,
    detail: str = "",
) -> None:
    """Move a message to dead_letter/ with a structured .reason.json sidecar.

    Used by both MessageProcessor and TTLSweeper for consistent dead-lettering.

    If *msg_id* and *timestamp* are provided (parsed envelope), the
    destination is built from envelope fields.  Otherwise falls back to
    extracting the filename from *msg_path* (raw/unparseable messages).

    A ``.reason.json`` sidecar is written alongside the dead-lettered
    message for programmatic triage.
    """
    from nexus.contracts.types import OperationContext

    ctx = OperationContext(user_id="system", groups=[], zone_id=zone_id, is_system=True)

    try:
        if msg_id is not None and timestamp is not None:
            dest = message_path_in_dead_letter(agent_id, msg_id, timestamp)
        else:
            filename = msg_path.rsplit("/", 1)[-1]
            dest = f"{dead_letter_path(agent_id)}/{filename}"

        vfs.sys_rename(msg_path, dest, context=ctx)

        # Write structured .reason.json sidecar (best-effort)
        try:
            reason_data = json.dumps(
                {
                    "reason": reason.value,
                    "detail": detail,
                    "agent_id": agent_id,
                    "zone_id": zone_id,
                    "msg_id": msg_id,
                },
                indent=2,
            ).encode("utf-8")
            reason_path = dest + ".reason.json"
            vfs.write(reason_path, reason_data, context=ctx)
        except Exception:
            logger.debug(
                "Failed to write .reason.json for dead-lettered message at %s",
                dest,
                exc_info=True,
            )

        logger.info(
            "Message %s moved to dead_letter for agent %s (reason: %s, detail: %s)",
            msg_id or msg_path,
            agent_id,
            reason.value,
            detail,
        )
    except Exception:
        logger.error(
            "Failed to move message %s to dead_letter",
            msg_id or msg_path,
            exc_info=True,
        )

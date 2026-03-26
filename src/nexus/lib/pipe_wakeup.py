"""Generic DT_PIPE wakeup signal utility (Issue #3194).

Provides ``wait_for_signal()`` — the drain-and-process pattern for pipe-based
wakeup signals.  Used by both IPC (``PipeWakeupListener``) and sync
(``WriteBackService``) subsystems.

Lives in ``nexus.lib`` (not ``nexus.bricks.ipc``) so that system_services
can import it without violating the five-tier architecture boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def wait_for_signal(
    pipe_manager: Any,
    path: str,
    timeout: float | None = None,
) -> bool:
    """Wait for a DT_PIPE wakeup signal with drain-and-process pattern.

    Blocks until at least one signal arrives (or timeout expires), then drains
    all remaining signals so the caller processes once for all coalesced signals.

    Args:
        pipe_manager: PipeManager instance (duck-typed to avoid core imports).
        path: DT_PIPE path to listen on.
        timeout: Max seconds to wait. None = wait forever. 0 = non-blocking poll.

    Returns:
        True if at least one signal was received, False if timed out.
    """
    try:
        if timeout is not None:
            await asyncio.wait_for(pipe_manager.pipe_read(path), timeout=timeout)
        else:
            await pipe_manager.pipe_read(path)
    except TimeoutError:
        return False
    except Exception:
        # PipeClosedError, PipeNotFoundError, CancelledError — caller handles
        raise

    # Drain remaining signals (non-blocking)
    while True:
        try:
            await pipe_manager.pipe_read(path, blocking=False)
        except Exception:
            # PipeEmptyError — no more pending signals
            break
    return True

"""Runtime stack-dump diagnostics for server startup/debugging."""

from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys

logger = logging.getLogger(__name__)


def install_stack_dump_signal() -> None:
    """Register SIGUSR1 stack dumps when explicitly enabled.

    This is intentionally env-gated so production processes do not change signal
    behavior unless an operator asks for diagnostics. It is useful for hangs
    where HTTP debug endpoints cannot run because the event loop is stuck.
    """
    enabled = os.environ.get("NEXUS_DEBUG_STACK_DUMP", "").lower() in {"1", "true", "yes"}
    if not enabled:
        return

    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        logger.warning("NEXUS_DEBUG_STACK_DUMP requested, but SIGUSR1 is unavailable")
        return

    faulthandler.enable(file=sys.stderr, all_threads=True)
    try:
        faulthandler.register(sigusr1, file=sys.stderr, all_threads=True, chain=False)
    except RuntimeError as exc:
        logger.warning("Failed to register SIGUSR1 stack dump handler: %s", exc)
        return

    logger.warning("Stack dump diagnostics enabled; send SIGUSR1 to dump all thread stacks")

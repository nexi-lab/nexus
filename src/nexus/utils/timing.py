"""Timing utilities for performance instrumentation (Issue #1287).

Replaces ~28 inline ``import time; start = time.time(); ... elapsed = time.time() - start``
blocks scattered across search_service.py and other modules.

Usage::

    from nexus.utils.timing import Timer

    with Timer() as t:
        result = metadata.list(path, recursive=True)
    logger.debug("[LIST] metadata.list(): %.1fms, %d files", t.ms, len(result))

    # Or with auto-logging:
    with Timer.log(logger, "[GREP] parallel search"):
        results = executor.submit(search_chunk, chunk)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager


class Timer:
    """Lightweight timing context manager.

    Attributes:
        elapsed: Elapsed time in seconds (float).
        ms: Elapsed time in milliseconds (float).
    """

    __slots__ = ("_start", "elapsed", "ms")

    def __enter__(self) -> Timer:
        self._start = time.monotonic()
        self.elapsed = 0.0
        self.ms = 0.0
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed = time.monotonic() - self._start
        self.ms = self.elapsed * 1000

    @staticmethod
    @contextmanager
    def log(
        target_logger: logging.Logger,
        label: str,
        level: int = logging.DEBUG,
    ) -> Generator[Timer, None, None]:
        """Context manager that auto-logs elapsed time on exit.

        Args:
            target_logger: Logger to write to.
            label: Prefix for the log message (e.g., "[LIST] metadata.list()").
            level: Log level (default: DEBUG).

        Yields:
            Timer instance (access .ms or .elapsed after the block).
        """
        t = Timer()
        t.__enter__()
        try:
            yield t
        finally:
            t.__exit__(None, None, None)
            target_logger.log(level, "%s: %.1fms", label, t.ms)

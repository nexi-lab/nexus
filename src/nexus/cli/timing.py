"""CLI timing infrastructure for per-command latency measurement.

Provides a context-manager based approach to measuring wall-clock, connection,
and server processing phases independently.

Usage::

    timing = CommandTiming()

    with timing.phase("connect"):
        nx = get_filesystem(config)

    with timing.phase("server"):
        result = nx.sys_readdir(path)

    # timing.phases == {"connect": 12.3, "server": 45.6}
    # timing.total_ms == 57.9
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class CommandTiming:
    """Accumulates phase-level timing for a single CLI command invocation."""

    phases: dict[str, float] = field(default_factory=dict)
    _start: float = field(default_factory=time.perf_counter, repr=False)

    @contextmanager
    def phase(self, name: str) -> Generator[None, None, None]:
        """Time a named phase (e.g. 'connect', 'server', 'render').

        Durations are stored in milliseconds.
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.phases[name] = round(elapsed_ms, 2)

    @property
    def total_ms(self) -> float:
        """Total wall-clock time since this CommandTiming was created."""
        return round((time.perf_counter() - self._start) * 1000, 2)

    def to_dict(self) -> dict[str, object]:
        """Serialize timing for JSON output."""
        return {
            "total_ms": self.total_ms,
            "phases": dict(self.phases),
        }

    def format_short(self) -> str:
        """Short timing string for -v output: ``[42ms total (server: 35ms)]``."""
        server_ms = self.phases.get("server")
        total = self.total_ms
        if server_ms is not None:
            return f"[{total:.0f}ms total (server: {server_ms:.0f}ms)]"
        return f"[{total:.0f}ms]"

    def format_breakdown(self) -> str:
        """Full breakdown for -vvv output."""
        lines = [f"  total: {self.total_ms:.1f}ms"]
        for name, ms in self.phases.items():
            lines.append(f"  {name}: {ms:.1f}ms")
        overhead = self.total_ms - sum(self.phases.values())
        if overhead > 0.5:
            lines.append(f"  cli_overhead: {overhead:.1f}ms")
        return "\n".join(lines)


def timing_enabled(verbosity: int) -> bool:
    """Check whether timing should be displayed.

    Timing is shown when:
    - verbosity >= 1 (``-v``)
    - ``NEXUS_TIMING=1`` environment variable is set
    """
    if verbosity >= 1:
        return True
    return os.environ.get("NEXUS_TIMING", "") == "1"

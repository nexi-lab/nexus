"""Resource pressure monitoring for agent eviction (Issue #2170).

Polls system memory usage via psutil (optional dependency) and reports
pressure levels used by the EvictionManager to trigger eviction cycles.

Gracefully degrades to NORMAL if psutil is not installed.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from nexus.contracts.qos import PressureLevel

if TYPE_CHECKING:
    from nexus.lib.performance_tuning import EvictionTuning

logger = logging.getLogger(__name__)

# Re-export for backward compatibility (moved to contracts.qos in Issue #2171)
__all__ = ["PressureLevel", "ResourceMonitor"]

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment,unused-ignore]
    _HAS_PSUTIL = False


class ResourceMonitor:
    """Monitor system resource pressure for eviction decisions.

    Uses psutil.virtual_memory() via asyncio.to_thread to avoid blocking
    the event loop. Falls back to NORMAL if psutil is unavailable.

    Args:
        tuning: EvictionTuning with watermark thresholds.
    """

    def __init__(self, tuning: "EvictionTuning") -> None:
        self._high_watermark = tuning.memory_high_watermark_pct
        self._low_watermark = tuning.memory_low_watermark_pct

    async def check_pressure(self) -> PressureLevel:
        """Check current resource pressure level.

        Returns:
            PressureLevel based on memory usage vs watermarks.
        """
        mem_pct = await self.get_memory_percent()
        if mem_pct < 0:
            return PressureLevel.NORMAL

        if mem_pct >= self._high_watermark:
            return PressureLevel.CRITICAL
        if mem_pct >= self._low_watermark:
            return PressureLevel.WARNING
        return PressureLevel.NORMAL

    async def get_memory_percent(self) -> float:
        """Get raw memory usage percentage.

        Returns:
            Memory usage as a percentage (0-100), or -1.0 if unavailable.
        """
        if not _HAS_PSUTIL:
            return -1.0

        try:
            mem = await asyncio.to_thread(psutil.virtual_memory)
            return float(mem.percent)
        except Exception:
            logger.warning("[RESOURCE] psutil.virtual_memory() failed, falling back to NORMAL")
            return -1.0

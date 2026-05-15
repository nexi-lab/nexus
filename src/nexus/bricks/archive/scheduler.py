"""Cron-driven archive scheduler with GFS retention sweep (#3793).

Hub-only: registered into the lifespan only when the active profile is hub.
Lightweight profile skips registration entirely.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.bricks.archive.retention import RetentionPolicy, apply_retention

if TYPE_CHECKING:
    from nexus.bricks.archive.orchestrator import ArchiveOrchestrator
    from nexus.bricks.archive.storage.base import ArchiveStorage

logger = logging.getLogger(__name__)


@dataclass
class ScheduleConfig:
    cron: str  # e.g. "0 2 * * *"
    policy: RetentionPolicy
    zones: list[str] | None = None  # None = all zones


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    if field == "*":
        return set(range(lo, hi + 1))
    out: set[int] = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            base_set = _parse_cron_field(base if base != "" else "*", lo, hi)
            out |= {v for v in base_set if (v - lo) % int(step) == 0}
        elif "-" in part:
            a, b = part.split("-", 1)
            out |= set(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


class ArchiveScheduler:
    """Polls every minute, runs the orchestrator on cron match, prunes per policy."""

    def __init__(
        self,
        cfg: ScheduleConfig,
        *,
        orchestrator: "ArchiveOrchestrator",
        storage: "ArchiveStorage",
    ) -> None:
        self.cfg = cfg
        self.orchestrator = orchestrator
        self.storage = storage
        m, h, dom, mon, dow = cfg.cron.split()
        self._minutes = _parse_cron_field(m, 0, 59)
        self._hours = _parse_cron_field(h, 0, 23)
        self._doms = _parse_cron_field(dom, 1, 31)
        self._months = _parse_cron_field(mon, 1, 12)
        self._dows = _parse_cron_field(dow, 0, 6)

    def _is_due(self, now: datetime) -> bool:
        return (
            now.minute in self._minutes
            and now.hour in self._hours
            and now.day in self._doms
            and now.month in self._months
            and (now.weekday() + 1) % 7 in self._dows
        )

    async def run_once(self, *, now: datetime) -> None:
        if not self._is_due(now):
            return
        try:
            manifests = self.orchestrator.create_archives(
                zone_ids=self.cfg.zones,
                strip=True,
                sign=True,
            )
            for manifest in manifests:
                output = Path(self.orchestrator.output_dir) / f"{manifest.source_zone_id}.nexus"
                if output.exists():
                    self.storage.put(output.name, output)
        except Exception:
            logger.exception("archive create failed")
            return

        try:
            entries = self.storage.list("")
            keep, prune = apply_retention(entries, self.cfg.policy, now=now)
            for e in prune:
                self.storage.delete(e.key)
            logger.info("archive retention: kept=%d pruned=%d", len(keep), len(prune))
        except Exception:
            logger.exception("archive retention sweep failed")

    async def run_forever(self) -> None:
        import datetime as _dt

        while True:
            now = datetime.now(tz=_dt.UTC)
            await self.run_once(now=now)
            await asyncio.sleep(60 - now.second)


__all__ = ["ScheduleConfig", "ArchiveScheduler"]

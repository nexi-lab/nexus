"""Env-driven configuration for the activity subsystem."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _parse_int(name: str, raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _parse_float(name: str, raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float, got {raw!r}") from exc


@dataclass(frozen=True)
class ActivityConfig:
    enabled: bool = True
    db_path: Path = Path("./activity.db")
    retention_days: int = 30
    queue_size: int = 10_000
    batch_size: int = 200
    batch_timeout_s: float = 0.5

    def __post_init__(self) -> None:
        # Bounded-queue contract: a non-positive queue_size disables
        # asyncio.Queue back-pressure (treated as unbounded), which would let
        # bursts grow memory without hitting the drop counter. Reject early
        # with a clear error rather than silently breaking the contract.
        if self.queue_size <= 0:
            raise ValueError(f"NEXUS_ACTIVITY_QUEUE_SIZE must be > 0, got {self.queue_size}")
        if self.batch_size <= 0:
            raise ValueError(f"NEXUS_ACTIVITY_BATCH_SIZE must be > 0, got {self.batch_size}")
        if self.batch_timeout_s <= 0 or not math.isfinite(self.batch_timeout_s):
            # NaN passes <= 0 (NaN comparisons are False); inf would prevent
            # partial-batch flushes. Both break the worker contract.
            raise ValueError(
                "NEXUS_ACTIVITY_BATCH_TIMEOUT_S must be a finite positive "
                f"float, got {self.batch_timeout_s}"
            )
        if self.retention_days < 0:
            raise ValueError(
                f"NEXUS_ACTIVITY_RETENTION_DAYS must be >= 0, got {self.retention_days}"
            )

    @classmethod
    def from_env(cls) -> ActivityConfig:
        data_dir = os.environ.get("NEXUS_DATA_DIR", ".")
        default_db = Path(data_dir) / "activity.db"
        return cls(
            enabled=_parse_bool(os.environ.get("NEXUS_ACTIVITY_ENABLED"), True),
            db_path=Path(os.environ.get("NEXUS_ACTIVITY_DB_PATH", str(default_db))),
            retention_days=_parse_int(
                "NEXUS_ACTIVITY_RETENTION_DAYS",
                os.environ.get("NEXUS_ACTIVITY_RETENTION_DAYS"),
                30,
            ),
            queue_size=_parse_int(
                "NEXUS_ACTIVITY_QUEUE_SIZE",
                os.environ.get("NEXUS_ACTIVITY_QUEUE_SIZE"),
                10_000,
            ),
            batch_size=_parse_int(
                "NEXUS_ACTIVITY_BATCH_SIZE",
                os.environ.get("NEXUS_ACTIVITY_BATCH_SIZE"),
                200,
            ),
            batch_timeout_s=_parse_float(
                "NEXUS_ACTIVITY_BATCH_TIMEOUT_S",
                os.environ.get("NEXUS_ACTIVITY_BATCH_TIMEOUT_S"),
                0.5,
            ),
        )

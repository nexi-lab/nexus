"""Env-driven configuration for the activity subsystem."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from nexus.contracts.protocols.activity import EventKind


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


def _validate_rate(name: str, rate: float) -> None:
    # NaN fails both comparisons, inf fails the upper bound — no separate
    # isfinite check needed.
    if not (0.0 <= rate <= 1.0):
        raise ValueError(f"{name} must be in [0.0, 1.0], got {rate}")


def _parse_sample_rates(raw: str | None) -> dict[str, float]:
    """Parse 'kind=rate,kind=rate' (e.g. 'search=0.05,mcp_tool_call=0.2')."""
    if raw is None or not raw.strip():
        return {}
    rates: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"NEXUS_ACTIVITY_SAMPLE_RATES entries must be kind=rate, got {part!r}")
        rates[key] = _parse_float("NEXUS_ACTIVITY_SAMPLE_RATES", value.strip(), 1.0)
    return rates


@dataclass(frozen=True)
class ActivityConfig:
    enabled: bool = True
    db_path: Path = Path("./activity.db")
    retention_days: int = 30
    queue_size: int = 10_000
    batch_size: int = 200
    batch_timeout_s: float = 0.5
    agent_log_enabled: bool = True
    agent_log_cap_bytes: int = 10 * 1024 * 1024
    agent_log_retention_days: int = 7
    agent_log_cmd_max_bytes: int = 4 * 1024
    segment_dir: Path = Path("./activity")
    min_free_mb: int = 1024
    sample_rate: float = 1.0
    sample_rates: dict[str, float] = field(default_factory=dict)

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
        if self.agent_log_cap_bytes <= 0:
            raise ValueError(
                f"NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES must be > 0, got {self.agent_log_cap_bytes}"
            )
        if self.agent_log_retention_days < 0:
            raise ValueError(
                f"NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS must be >= 0, "
                f"got {self.agent_log_retention_days}"
            )
        if self.agent_log_cmd_max_bytes <= 0:
            raise ValueError(
                f"NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES must be > 0, "
                f"got {self.agent_log_cmd_max_bytes}"
            )
        if self.min_free_mb < 0:
            raise ValueError(f"NEXUS_ACTIVITY_MIN_FREE_MB must be >= 0, got {self.min_free_mb}")
        _validate_rate("NEXUS_ACTIVITY_SAMPLE_RATE", self.sample_rate)
        valid_kinds = {k.value for k in EventKind}
        for key, rate in self.sample_rates.items():
            if key not in valid_kinds:
                raise ValueError(
                    f"NEXUS_ACTIVITY_SAMPLE_RATES has unknown event kind {key!r}; "
                    f"valid kinds: {sorted(valid_kinds)}"
                )
            _validate_rate(f"NEXUS_ACTIVITY_SAMPLE_RATES[{key!r}]", rate)

    @classmethod
    def from_env(cls) -> ActivityConfig:
        data_dir = os.environ.get("NEXUS_DATA_DIR", ".")
        default_db = Path(data_dir) / "activity.db"
        db_path = Path(os.environ.get("NEXUS_ACTIVITY_DB_PATH", str(default_db)))
        # Segments default to a sibling dir of the (possibly operator-moved)
        # db_path so a dedicated telemetry volume keeps everything together.
        default_segment_dir = db_path.parent / "activity"
        return cls(
            enabled=_parse_bool(os.environ.get("NEXUS_ACTIVITY_ENABLED"), True),
            db_path=db_path,
            segment_dir=Path(os.environ.get("NEXUS_ACTIVITY_DIR", str(default_segment_dir))),
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
            agent_log_enabled=_parse_bool(os.environ.get("NEXUS_ACTIVITY_AGENT_LOG_ENABLED"), True),
            agent_log_cap_bytes=_parse_int(
                "NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES",
                os.environ.get("NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES"),
                10 * 1024 * 1024,
            ),
            agent_log_retention_days=_parse_int(
                "NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS",
                os.environ.get("NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS"),
                7,
            ),
            agent_log_cmd_max_bytes=_parse_int(
                "NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES",
                os.environ.get("NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES"),
                4 * 1024,
            ),
            min_free_mb=_parse_int(
                "NEXUS_ACTIVITY_MIN_FREE_MB",
                os.environ.get("NEXUS_ACTIVITY_MIN_FREE_MB"),
                1024,
            ),
            sample_rate=_parse_float(
                "NEXUS_ACTIVITY_SAMPLE_RATE",
                os.environ.get("NEXUS_ACTIVITY_SAMPLE_RATE"),
                1.0,
            ),
            sample_rates=_parse_sample_rates(os.environ.get("NEXUS_ACTIVITY_SAMPLE_RATES")),
        )

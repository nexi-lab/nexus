"""Static configuration for the approvals brick."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApprovalConfig:
    enabled: bool = False
    auto_deny_after_seconds: float = 60.0
    auto_deny_max_seconds: float = 600.0
    sweeper_interval_seconds: float = 5.0
    watch_buffer_size: int = 256
    diag_dump_history_limit: int = 100
    # F3 (#3790): periodic reconcile watchdog interval. Even when LISTEN/NOTIFY
    # delivers reliably, a brief asyncpg listener disconnect can strand local
    # waiters for rows decided on a remote worker. The watchdog runs
    # ``reconcile_in_flight`` on this cadence so cross-worker decisions
    # always converge regardless of NOTIFY health. Set to <= 0 to disable
    # (tests-only — production should keep the default).
    reconcile_interval_seconds: float = 30.0

    def clamp_request_timeout(self, requested: float | None) -> float:
        if requested is None:
            return self.auto_deny_after_seconds
        if requested <= 0:
            raise ValueError(f"timeout must be > 0, got {requested}")
        return min(requested, self.auto_deny_max_seconds)

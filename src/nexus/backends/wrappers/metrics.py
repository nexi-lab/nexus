"""WrapperMetrics — shared OTel metrics helper for Backend wrappers (#1705).

Provides lazy-initialized OTel counters and thread-safe stat tracking
for all recursive wrappers (EncryptedStorage,
CompressedStorage, etc.).

Composition-based: each wrapper owns a WrapperMetrics instance rather
than inheriting from a mixin (LEGO Architecture Principle #5:
composition over inheritance).

Usage:
    metrics = WrapperMetrics(
        meter_name="nexus.encrypted_storage",
        counter_names=["encrypt_ops", "decrypt_ops", "errors"],
    )
    metrics.increment("encrypt_ops")
    stats = metrics.get_stats()  # {"encrypt_ops": 5, "decrypt_ops": 3, ...}

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Recursive Wrapping (Mechanism 2)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
"""

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class WrapperMetrics:
    """Thread-safe OTel metrics helper for Backend wrappers.

    Lazy-initializes OTel counters on first increment. Falls back to
    in-memory counters when OTel is unavailable or disabled.

    Args:
        meter_name: OTel meter name (e.g., "nexus.encrypted_storage").
        counter_names: List of counter names to create.
        enabled: Whether metrics collection is enabled. When False,
            increments are still tracked in-memory but OTel is skipped.
    """

    def __init__(
        self,
        meter_name: str,
        counter_names: list[str],
        *,
        enabled: bool = True,
    ) -> None:
        self._meter_name = meter_name
        self._counter_names = counter_names
        self._enabled = enabled

        # In-memory counters (always active for get_stats())
        self._lock = threading.Lock()
        self._counts: dict[str, int] = dict.fromkeys(counter_names, 0)

        # OTel counters (lazy-initialized)
        self._otel_counters: dict[str, Any] | None = None
        self._otel_initialized = False

    def increment(self, name: str, delta: int = 1) -> None:
        """Increment a named counter.

        Thread-safe. Increments both the in-memory counter and the OTel
        counter (if initialized).

        Args:
            name: Counter name (must be in counter_names from __init__).
            delta: Amount to increment (default 1).
        """
        with self._lock:
            if name in self._counts:
                self._counts[name] += delta

        if self._enabled:
            otel = self._get_otel_counters()
            if otel is not None and name in otel:
                otel[name].add(delta)

    def get_stats(self) -> dict[str, int]:
        """Return a snapshot of all counter values.

        Returns:
            Dict mapping counter names to their current values.
        """
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        """Reset all in-memory counters to zero.

        OTel counters are monotonic and cannot be reset.
        """
        with self._lock:
            for name in self._counts:
                self._counts[name] = 0

    def _get_otel_counters(self) -> dict[str, Any] | None:
        """Lazy-init OTel counters. Returns None if OTel is unavailable.

        Uses double-checked locking for thread safety. The OTel
        initialization is performed entirely inside the lock to prevent
        another thread from seeing ``_otel_initialized = True`` before
        ``_otel_counters`` is populated.

        NOTE: The unsynchronized read of ``_otel_initialized`` (line below)
        relies on CPython's GIL making attribute reads/writes atomic at the
        bytecode level. If targeting free-threaded Python (PEP 703), this
        must be replaced with ``threading.Lock``-only access or atomics.
        """
        if self._otel_initialized:
            return self._otel_counters

        with self._lock:
            if self._otel_initialized:
                return self._otel_counters

            try:
                from nexus.lib.telemetry import is_telemetry_enabled

                if not is_telemetry_enabled():
                    return None

                from opentelemetry import metrics

                meter = metrics.get_meter(self._meter_name)
                self._otel_counters = {
                    name: meter.create_counter(f"{self._meter_name}.{name}")
                    for name in self._counter_names
                }
            except Exception as e:
                logger.debug("OTel counter init failed for %s: %s", self._meter_name, e)
            finally:
                self._otel_initialized = True

            return self._otel_counters

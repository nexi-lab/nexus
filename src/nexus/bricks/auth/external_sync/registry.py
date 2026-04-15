"""AdapterRegistry — lifecycle, startup sync, and background refresh for external CLI adapters.

Manages a set of ExternalCliSyncAdapter instances with per-adapter circuit
breakers. Provides:
  - startup(): concurrent detect+sync with global timeout
  - run_refresh_loop(): background refresh respecting TTL and breaker state
  - CircuitBreaker: dataclass tracking failures and half-open recovery
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncResult,
)
from nexus.bricks.auth.profile import (
    AuthProfile,
    AuthProfileStore,
    ProfileUsageStats,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Circuit breaker (per-adapter)
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    """Per-adapter circuit breaker with failure counting and half-open recovery.

    States:
      - Closed: failure_count < failure_threshold (normal operation)
      - Open (tripped): failure_count >= threshold AND reset timeout NOT elapsed
      - Half-open: tripped AND reset timeout elapsed (allow one probe)
    """

    failure_threshold: int = 3
    reset_timeout_seconds: float = 60.0
    failure_count: int = field(default=0, init=False)
    tripped_at: float | None = field(default=None, init=False)  # time.monotonic()

    @property
    def is_tripped(self) -> bool:
        """True if open and reset timeout NOT elapsed."""
        if self.tripped_at is None:
            return False
        return (time.monotonic() - self.tripped_at) < self.reset_timeout_seconds

    @property
    def is_half_open(self) -> bool:
        """True if tripped but reset timeout elapsed (allow probe)."""
        if self.tripped_at is None:
            return False
        return (time.monotonic() - self.tripped_at) >= self.reset_timeout_seconds

    def record_success(self) -> None:
        self.failure_count = 0
        self.tripped_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.tripped_at = time.monotonic()


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """Manages adapter lifecycle, startup sync, and background refresh.

    Each adapter gets its own CircuitBreaker. The registry handles:
      - Concurrent startup with a global timeout
      - Background refresh loop respecting TTL and breaker state
      - Upserting sync results into the AuthProfileStore
    """

    def __init__(
        self,
        adapters: list[ExternalCliSyncAdapter],
        profile_store: AuthProfileStore,
        *,
        startup_timeout: float = 3.0,
        loop_tick_seconds: float = 30.0,
    ) -> None:
        self._adapters: dict[str, ExternalCliSyncAdapter] = {a.adapter_name: a for a in adapters}
        self._store = profile_store
        self._startup_timeout = startup_timeout
        self._loop_tick_seconds = loop_tick_seconds
        self._breakers: dict[str, CircuitBreaker] = {
            a.adapter_name: CircuitBreaker(
                failure_threshold=a.failure_threshold,
                reset_timeout_seconds=a.reset_timeout_seconds,
            )
            for a in adapters
        }
        self._last_sync_times: dict[str, float] = {}

    def get_adapter(self, adapter_name: str) -> ExternalCliSyncAdapter | None:
        """Return the adapter for the given name, or None."""
        return self._adapters.get(adapter_name)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def startup(self) -> dict[str, SyncResult]:
        """Concurrent detect+sync for all adapters with a global timeout.

        Decision 15A: asyncio.gather + global timeout. Adapters that miss the
        timeout get SyncResult(error="timeout"). Successful results are
        upserted into the store immediately.
        """
        results: dict[str, SyncResult] = {}

        async def _detect_and_sync(name: str, adapter: ExternalCliSyncAdapter) -> SyncResult:
            detected = await adapter.detect()
            if not detected:
                return SyncResult(adapter_name=name, error="not detected")
            return await adapter.sync()

        tasks = {
            name: asyncio.create_task(_detect_and_sync(name, adapter))
            for name, adapter in self._adapters.items()
        }

        if tasks:
            # Use asyncio.wait with a timeout — it never cancels tasks itself,
            # so we can inspect each one individually afterwards.
            done, pending = await asyncio.wait(
                tasks.values(),
                timeout=self._startup_timeout,
            )

            # Cancel tasks that didn't finish in time
            for task in pending:
                task.cancel()
            # Suppress CancelledError from cancelled tasks
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        for name, task in tasks.items():
            breaker = self._breakers[name]
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    result = SyncResult(adapter_name=name, error=str(exc))
                    breaker.record_failure()
                else:
                    result = task.result()
                    if result.error is not None:
                        breaker.record_failure()
                    else:
                        breaker.record_success()
                        self._upsert_sync_results(result)
                        self._last_sync_times[name] = time.monotonic()
            else:
                if not task.done():
                    task.cancel()
                result = SyncResult(adapter_name=name, error="timeout")
                breaker.record_failure()
            results[name] = result

        return results

    # ------------------------------------------------------------------
    # Background refresh loop
    # ------------------------------------------------------------------

    async def run_refresh_loop(self) -> None:
        """Background refresh loop. Cancel via task.cancel().

        Every loop_tick_seconds:
          1. Skip tripped (not half-open) breakers
          2. Skip adapters not past TTL
          3. Sync, upsert, update breaker
        """
        while True:
            await asyncio.sleep(self._loop_tick_seconds)
            for name, adapter in self._adapters.items():
                breaker = self._breakers[name]
                # Skip fully tripped breakers (not half-open)
                if breaker.is_tripped and not breaker.is_half_open:
                    continue
                # Check TTL
                last = self._last_sync_times.get(name)
                if last is not None:
                    elapsed = time.monotonic() - last
                    if elapsed < adapter.sync_ttl_seconds:
                        continue
                await self._sync_adapter(adapter)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _sync_adapter(self, adapter: ExternalCliSyncAdapter) -> SyncResult:
        """Sync one adapter, update breaker and store."""
        name = adapter.adapter_name
        breaker = self._breakers[name]
        try:
            result = await adapter.sync()
        except Exception as exc:
            result = SyncResult(adapter_name=name, error=str(exc))

        if result.error is not None:
            breaker.record_failure()
        else:
            breaker.record_success()
            self._upsert_sync_results(result)
            self._last_sync_times[name] = time.monotonic()

        return result

    def _upsert_sync_results(self, result: SyncResult) -> None:
        """Map SyncedProfile -> AuthProfile and upsert into the store.

        - backend = "external-cli"
        - backend_key = sp.backend_key (already formatted by the adapter)
        - Preserve existing usage_stats if profile already in store
        - Use ProfileUsageStats() for new profiles
        """
        now = datetime.now(UTC)
        for sp in result.profiles:
            profile_id = f"{sp.provider}/{sp.account_identifier}"
            existing = self._store.get(profile_id)
            usage_stats = existing.usage_stats if existing is not None else ProfileUsageStats()

            # Look up the adapter to get its sync_ttl_seconds
            adapter = self._adapters.get(result.adapter_name)
            sync_ttl = int(adapter.sync_ttl_seconds) if adapter else 300

            profile = AuthProfile(
                id=profile_id,
                provider=sp.provider,
                account_identifier=sp.account_identifier,
                backend="external-cli",
                backend_key=sp.backend_key,
                last_synced_at=now,
                sync_ttl_seconds=sync_ttl,
                usage_stats=usage_stats,
            )
            self._store.upsert(profile)

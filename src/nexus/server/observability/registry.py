"""ObservabilityRegistry — unified lifecycle management for observability components.

Issue #2072: Single registry managing all observability component lifecycles,
following the OTel Collector component.Component interface pattern.

Components are started in registration order and shut down in reverse (LIFO).
Required components abort startup on failure (rolling back already-started).
Optional components log and continue on failure.
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class LifecycleComponent(Protocol):
    """Protocol for observability components with managed lifecycle.

    Matches the OTel Collector component.Component interface pattern.
    """

    @property
    def name(self) -> str:
        """Human-readable component name."""
        ...

    async def start(self) -> None:
        """Initialize the component. Called once during startup."""
        ...

    async def shutdown(self, timeout_ms: int = 5000) -> None:
        """Gracefully shut down the component."""
        ...

    def is_healthy(self) -> bool:
        """Return True if the component is functioning normally."""
        ...


@dataclass(frozen=True)
class ComponentStatus:
    """Health status snapshot for a single component."""

    name: str
    started: bool
    healthy: bool
    error: str | None = None


@dataclass
class ObservabilityRegistry:
    """Manages observability component lifecycles with ordered startup/shutdown.

    - Registration order = startup order
    - Shutdown is reverse of registration (LIFO)
    - Required component failure during start -> rollback already-started, raise
    - Optional component failure during start -> log and continue
    - shutdown_all() is idempotent
    - shutdown_all() safe to call without start_all()
    """

    _components: list[tuple[str, LifecycleComponent, bool]] = field(default_factory=list)
    _started: list[str] = field(default_factory=list)
    _shutdown_called: bool = False

    def register(self, name: str, component: LifecycleComponent, *, required: bool = False) -> None:
        """Register a component for lifecycle management.

        Args:
            name: Unique component identifier.
            component: The lifecycle component instance.
            required: If True, startup failure aborts and rolls back.

        Raises:
            ValueError: If a component with the same name is already registered.
        """
        existing_names = {n for n, _, _ in self._components}
        if name in existing_names:
            raise ValueError(f"Component {name!r} already registered")
        self._components.append((name, component, required))

    async def start_all(self) -> list[ComponentStatus]:
        """Start all registered components in registration order.

        Returns:
            List of ComponentStatus for each component.

        Raises:
            RuntimeError: If a required component fails to start.
        """
        statuses: list[ComponentStatus] = []

        for name, component, required in self._components:
            t0 = time.perf_counter()
            try:
                await component.start()
                elapsed_ms = (time.perf_counter() - t0) * 1000
                self._started.append(name)
                statuses.append(ComponentStatus(name=name, started=True, healthy=True))
                logger.info("Started observability component: %s (%.1fms)", name, elapsed_ms)
            except Exception as exc:
                error_msg = str(exc)
                statuses.append(
                    ComponentStatus(name=name, started=False, healthy=False, error=error_msg)
                )

                if required:
                    logger.error(
                        "Required observability component %s failed to start: %s", name, exc
                    )
                    # Rollback already-started components
                    await self._rollback_started()
                    raise RuntimeError(
                        f"Required component {name!r} failed to start: {exc}"
                    ) from exc

                logger.info("Optional observability component %s failed to start: %s", name, exc)

        return statuses

    async def shutdown_all(self, timeout_ms: int = 5000) -> None:
        """Shut down all started components in reverse order. Idempotent.

        Note: No lock needed — asyncio is single-threaded so this boolean
        check is safe against concurrent coroutines.
        """
        if self._shutdown_called:
            return
        self._shutdown_called = True

        # Shutdown in reverse order (LIFO)
        component_map = {name: comp for name, comp, _ in self._components}
        for name in reversed(self._started):
            component = component_map.get(name)
            if component is None:
                continue
            try:
                await component.shutdown(timeout_ms=timeout_ms)
                logger.info("Shut down observability component: %s", name)
            except Exception as exc:
                logger.warning("Error shutting down observability component %s: %s", name, exc)

        self._started.clear()

    def status(self) -> list[ComponentStatus]:
        """Return health status of all registered components."""
        result: list[ComponentStatus] = []
        for name, component, _ in self._components:
            started = name in self._started
            try:
                healthy = component.is_healthy() if started else False
            except Exception:
                healthy = False
            result.append(ComponentStatus(name=name, started=started, healthy=healthy))
        return result

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Context manager that starts all components and shuts them down on exit."""
        await self.start_all()
        try:
            yield
        finally:
            await self.shutdown_all()

    async def _rollback_started(self) -> None:
        """Shut down already-started components during a failed startup."""
        component_map = {name: comp for name, comp, _ in self._components}
        for name in reversed(self._started):
            component = component_map.get(name)
            if component is None:
                continue
            try:
                await component.shutdown()
                logger.info("Rolled back observability component: %s", name)
            except Exception as exc:
                logger.warning("Error rolling back observability component %s: %s", name, exc)
        self._started.clear()

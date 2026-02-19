"""Provider registry for sandbox providers (Issue #2051: Decompose SandboxManager).

Manages provider lifecycle: registration, lazy initialization, availability
checks, and auto-selection. Replaces the provider initialization logic that
was previously embedded in SandboxManager.__init__.

Supports both eager registration (provider already created) and lazy
registration (factory callable invoked on first use).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from nexus.bricks.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)

# Auto-selection preference order for sandbox creation.
# Monty is excluded: it's an in-process executor used only by SandboxRouter
# for code execution routing, not for standalone sandbox creation.
_AUTO_SELECT_ORDER = ("docker", "e2b")


class ProviderRegistry:
    """Registry for sandbox providers with lazy initialization support.

    Providers can be registered eagerly (already created) or lazily
    (factory callable). Lazy providers are initialized on first ``get()``.

    Thread-safety: registration is expected at startup only. ``get()``
    may be called from async tasks but Python's GIL protects dict reads.
    """

    def __init__(self) -> None:
        self._providers: dict[str, SandboxProvider] = {}
        self._factories: dict[str, Callable[[], SandboxProvider]] = {}

    def register(self, name: str, provider: SandboxProvider) -> None:
        """Register an already-initialized provider.

        Args:
            name: Provider name ("docker", "e2b", "monty").
            provider: Provider instance.
        """
        self._providers[name] = provider
        self._factories.pop(name, None)
        logger.info("Registered sandbox provider: %s", name)

    def register_lazy(
        self, name: str, factory: Callable[[], SandboxProvider]
    ) -> None:
        """Register a provider factory for lazy initialization.

        The factory is called on the first ``get()`` for this provider.
        If the factory raises, the provider is NOT registered and the
        error propagates to the caller.

        Args:
            name: Provider name.
            factory: Callable that creates a SandboxProvider.
        """
        self._factories[name] = factory
        logger.debug("Registered lazy sandbox provider: %s", name)

    def get(self, name: str) -> SandboxProvider:
        """Get a provider by name, initializing lazily if needed.

        Args:
            name: Provider name.

        Returns:
            The provider instance.

        Raises:
            ValueError: If provider is not registered.
            RuntimeError: If lazy initialization fails.
        """
        # Check eagerly registered providers first
        if name in self._providers:
            return self._providers[name]

        # Try lazy initialization (factory stays until success for retry)
        factory = self._factories.get(name)
        if factory is not None:
            try:
                provider = factory()
                self._providers[name] = provider
                del self._factories[name]  # Remove only after success
                logger.info("Lazily initialized sandbox provider: %s", name)
                return provider
            except Exception:
                # Factory stays in _factories so next get() can retry
                logger.warning(
                    "Failed to lazily initialize provider '%s'", name
                )
                raise

        available = ", ".join(self.available_names()) or "none"
        raise ValueError(
            f"Provider '{name}' not available. Available providers: {available}"
        )

    def has(self, name: str) -> bool:
        """Check if a provider is registered (eager or lazy).

        Args:
            name: Provider name.

        Returns:
            True if registered.
        """
        return name in self._providers or name in self._factories

    def is_empty(self) -> bool:
        """Check if no providers are registered."""
        return not self._providers and not self._factories

    def available_names(self) -> list[str]:
        """Get names of all registered providers (eager + lazy).

        Returns:
            Sorted list of provider names.
        """
        names = set(self._providers.keys()) | set(self._factories.keys())
        return sorted(names)

    def auto_select(self) -> str:
        """Auto-select the best available provider for sandbox creation.

        Preference order: docker -> e2b -> first non-monty available.
        Monty is excluded (in-process only, used via SandboxRouter).

        Returns:
            Provider name.

        Raises:
            ValueError: If no providers are registered.
        """
        if self.is_empty():
            raise ValueError(
                "No sandbox providers available. Available providers: none"
            )

        for name in _AUTO_SELECT_ORDER:
            if self.has(name):
                return name

        # Fall back to first available non-monty provider
        for name in self.available_names():
            if name != "monty":
                return name

        available = ", ".join(self.available_names()) or "none"
        raise ValueError(
            f"No sandbox providers available. Available providers: {available}"
        )

    def items(self) -> list[tuple[str, SandboxProvider]]:
        """Get all eagerly initialized providers as (name, provider) pairs.

        Lazy providers are NOT initialized by this call.

        Returns:
            List of (name, provider) tuples.
        """
        return list(self._providers.items())

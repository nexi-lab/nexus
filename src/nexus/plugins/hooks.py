"""Plugin hooks system for lifecycle events."""

from collections.abc import Callable
from enum import StrEnum
from typing import Any


class HookType(StrEnum):
    """Available hook types for plugins."""

    BEFORE_WRITE = "before_write"
    AFTER_WRITE = "after_write"
    BEFORE_READ = "before_read"
    AFTER_READ = "after_read"
    BEFORE_DELETE = "before_delete"
    AFTER_DELETE = "after_delete"
    BEFORE_MKDIR = "before_mkdir"
    AFTER_MKDIR = "after_mkdir"
    BEFORE_COPY = "before_copy"
    AFTER_COPY = "after_copy"


class PluginHooks:
    """Registry for plugin hooks with priority ordering."""

    def __init__(self) -> None:
        """Initialize hooks registry."""
        self._hooks: dict[HookType, list[tuple[int, Callable]]] = {
            hook_type: [] for hook_type in HookType
        }

    def register(self, hook_type: HookType, handler: Callable, priority: int = 0) -> None:
        """Register a hook handler.

        Args:
            hook_type: Type of hook to register
            handler: Async callable to handle the hook
            priority: Priority (higher = executed first). Default: 0
        """
        if hook_type not in self._hooks:
            raise ValueError(f"Unknown hook type: {hook_type}")

        self._hooks[hook_type].append((priority, handler))
        # Sort by priority (descending)
        self._hooks[hook_type].sort(key=lambda x: x[0], reverse=True)

    def unregister(self, hook_type: HookType, handler: Callable) -> None:
        """Unregister a hook handler.

        Args:
            hook_type: Type of hook to unregister
            handler: Handler to remove
        """
        if hook_type not in self._hooks:
            return

        self._hooks[hook_type] = [(p, h) for p, h in self._hooks[hook_type] if h != handler]

    async def execute(self, hook_type: HookType, context: dict[str, Any]) -> dict[str, Any] | None:
        """Execute all handlers for a hook type.

        Args:
            hook_type: Type of hook to execute
            context: Context data passed to handlers

        Returns:
            Modified context or None if hook execution should stop
        """
        if hook_type not in self._hooks:
            return context

        for _priority, handler in self._hooks[hook_type]:
            try:
                result = await handler(context)
                if result is None:
                    # Handler returned None - stop execution
                    return None
                context = result
            except Exception as e:
                # Log error but continue with other hooks
                print(f"Hook {hook_type} handler {handler} failed: {e}")
                continue

        return context

    def get_handlers(self, hook_type: HookType) -> list[Callable]:
        """Get all handlers for a hook type.

        Args:
            hook_type: Type of hook

        Returns:
            List of handlers ordered by priority
        """
        if hook_type not in self._hooks:
            return []

        return [handler for _priority, handler in self._hooks[hook_type]]

    def clear(self, hook_type: HookType | None = None) -> None:
        """Clear all hooks or hooks of a specific type.

        Args:
            hook_type: Type of hook to clear. If None, clear all hooks.
        """
        if hook_type is None:
            for ht in HookType:
                self._hooks[ht] = []
        elif hook_type in self._hooks:
            self._hooks[hook_type] = []

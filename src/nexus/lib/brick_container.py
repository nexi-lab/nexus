"""Lightweight DI container for LEGO Architecture bricks (Issue #1393).

Maps Protocol types to their concrete implementations, enabling
bricks to depend on protocols rather than concrete classes.

Usage:
    from nexus.lib.brick_container import BrickContainer

    container = BrickContainer()
    container.register(AuthBrickProtocol, auth_brick)
    auth = container.resolve(AuthBrickProtocol)
"""

import logging
from typing import Any, TypeVar, cast

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BrickContainer:
    """DI container mapping Protocol types to brick implementations.

    Thread-safe for reads after initialization. Registration should happen
    during startup (single-threaded), resolution during request handling.
    """

    def __init__(self) -> None:
        self._registry: dict[type, Any] = {}

    def register(self, protocol_type: type[T], implementation: T) -> None:
        """Register a brick implementation for a protocol type.

        Args:
            protocol_type: The Protocol type to register against.
            implementation: The concrete implementation instance.

        Raises:
            TypeError: If implementation does not satisfy the protocol.
        """
        if not isinstance(implementation, protocol_type):
            raise TypeError(
                f"{type(implementation).__name__} does not satisfy {protocol_type.__name__}"
            )
        self._registry[protocol_type] = implementation
        logger.info(
            "Registered %s -> %s",
            protocol_type.__name__,
            type(implementation).__name__,
        )

    def resolve(self, protocol_type: type[T]) -> T:
        """Resolve a registered brick by protocol type.

        Args:
            protocol_type: The Protocol type to look up.

        Returns:
            The registered implementation.

        Raises:
            LookupError: If no implementation is registered.
        """
        impl = self._registry.get(protocol_type)
        if impl is None:
            raise LookupError(f"No implementation registered for {protocol_type.__name__}")
        return cast(T, impl)

    def resolve_optional(self, protocol_type: type[T]) -> T | None:
        """Resolve a registered brick, returning None if not found.

        Args:
            protocol_type: The Protocol type to look up.

        Returns:
            The registered implementation or None.
        """
        return cast("T | None", self._registry.get(protocol_type))

    def registered_protocols(self) -> list[type]:
        """Return all registered protocol types (for introspection)."""
        return list(self._registry.keys())

    def invalidate(self, protocol_type: type) -> None:
        """Remove a registration.

        Args:
            protocol_type: The Protocol type to remove.
        """
        removed = self._registry.pop(protocol_type, None)
        if removed is not None:
            logger.info("Invalidated %s", protocol_type.__name__)

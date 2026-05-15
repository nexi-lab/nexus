"""Lifecycle decorators for EventBus implementations."""

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def requires_started(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to ensure EventBus is started before method execution.

    Works with both async functions and async generators.

    Usage:
        class MyEventBus(EventBusBase):
            @requires_started
            async def publish(self, event):
                # Method only runs if self._started == True

            @requires_started
            async def subscribe(self, zone_id):
                # Async generator also supported
    """
    if inspect.isasyncgenfunction(func):
        # Handle async generators
        @functools.wraps(func)
        async def async_gen_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            if not self._started:
                raise RuntimeError(f"{self.__class__.__name__} not started. Call start() first.")
            async for item in func(self, *args, **kwargs):
                yield item

        return async_gen_wrapper
    else:
        # Handle regular async functions
        @functools.wraps(func)
        async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            if not self._started:
                raise RuntimeError(f"{self.__class__.__name__} not started. Call start() first.")
            return await func(self, *args, **kwargs)

        return async_wrapper

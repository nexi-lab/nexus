"""Invoke local or remote services without blocking MCP's event loop."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


async def call_service_method(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    result = await asyncio.to_thread(method, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result

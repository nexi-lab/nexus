import asyncio
import threading

import pytest

from nexus.bricks.mcp.service_calls import call_service_method
from nexus.lib.request_credentials import request_api_key


@pytest.mark.asyncio
async def test_sync_service_preserves_request_credential_without_blocking_the_loop():
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocking_service():
        credential = request_api_key.get()
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=2)
        return credential

    token = request_api_key.set("request-owner")
    try:
        task = asyncio.create_task(call_service_method(blocking_service))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not task.done()
        release.set()
        assert await task == "request-owner"
    finally:
        release.set()
        request_api_key.reset(token)


@pytest.mark.asyncio
async def test_async_service_keeps_the_callers_event_loop_and_credential():
    loop = asyncio.get_running_loop()

    async def service():
        assert asyncio.get_running_loop() is loop
        return request_api_key.get()

    token = request_api_key.set("async-owner")
    try:
        assert await call_service_method(service) == "async-owner"
    finally:
        request_api_key.reset(token)

"""Concurrent request ingress -> shared SearchDaemon -> real gRPC transport."""

import asyncio

import grpc
import httpx
import pytest
from fastapi import FastAPI

from nexus.bricks.search.daemon import SearchDaemon
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.lib.request_credentials import request_api_key
from nexus.runtime.zone_runner import ZoneRegistry
from nexus.server.middleware.request_credentials import RequestCredentialsMiddleware
from nexus.server.zone_execution import run_zone_scoped


@pytest.mark.asyncio
async def test_http_credentials_are_isolated_on_shared_channel():
    seen = []
    both = asyncio.Event()
    cancel_started = asyncio.Event()
    cancelled = asyncio.Event()

    class Receiver(search_pb2_grpc.SearchServiceServicer):
        async def Health(self, request, context):
            return search_pb2.HealthResponse()

        async def Stats(self, request, context):
            if request.zone_id == "cancel":
                cancel_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            seen.append((request.zone_id, dict(context.invocation_metadata()).get("authorization")))
            if len(seen) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 5)
            return search_pb2.StatsResponse()

    server = grpc.aio.server()
    search_pb2_grpc.add_SearchServiceServicer_to_server(Receiver(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    daemon = SearchDaemon(target=f"127.0.0.1:{port}")
    zones = ZoneRegistry()
    await daemon.get_health()
    app = FastAPI()
    app.add_middleware(RequestCredentialsMiddleware)

    @app.get("/stats")
    async def stats(zone_id: str):
        async def work():
            await daemon.get_stats(zone_id=zone_id)
            return {"ok": True}

        return await run_zone_scoped(zones, zone_id, work)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                client.get("/stats?zone_id=alice", headers={"Authorization": "Bearer sk-alice"}),
                client.get("/stats?zone_id=bob", headers={"Authorization": "sk-bob"}),
            )
            assert all(response.status_code == 200 for response in responses)
            assert sorted(seen) == [("alice", "Bearer sk-alice"), ("bob", "Bearer sk-bob")]
            pending = asyncio.create_task(
                client.get("/stats?zone_id=cancel", headers={"Authorization": "Bearer sk-cancel"})
            )
            await asyncio.wait_for(cancel_started.wait(), 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(cancelled.wait(), 5)
            assert request_api_key.get() is None
            await client.get("/stats?zone_id=anonymous")
            await client.get("/stats?zone_id=invalid", headers={"Authorization": "Basic abc"})
            await client.get(
                "/stats?zone_id=duplicate",
                headers=[("Authorization", "Bearer a"), ("Authorization", "Bearer b")],
            )
            assert seen[2:] == [
                ("anonymous", None),
                ("invalid", "Bearer "),
                ("duplicate", "Bearer "),
            ]
        await zones.runner_for("shutdown").call(daemon.shutdown)
    finally:
        await daemon.shutdown()
        await asyncio.to_thread(zones.stop_all)
        await server.stop(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_request_context_restored_after_failure(error):
    async def fail(scope, receive, send):
        assert request_api_key.get() == "sk-inner"
        raise error()

    outer = request_api_key.set("sk-outer")
    try:
        middleware = RequestCredentialsMiddleware(fail)
        with pytest.raises(error):
            await middleware(
                {"type": "http", "headers": [(b"authorization", b"Bearer sk-inner")]}, None, None
            )
        assert request_api_key.get() == "sk-outer"
    finally:
        request_api_key.reset(outer)

"""Structured per-request audit logging for MCP HTTP transport (#3779)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("nexus.mcp.audit")


def _emit_stdout_record(record: dict[str, Any]) -> None:
    """Emit a single audit line to stdout as JSON.

    Isolated so tests can monkeypatch it.
    """
    print(json.dumps(record, separators=(",", ":")), flush=True)


async def _publish_record(record: dict[str, Any]) -> None:
    """Publish the audit record to the Redis `nexus:audit:mcp` channel.

    Failures are swallowed by the caller (fire-and-forget). Isolated for
    test monkeypatching.
    """
    try:
        import redis.asyncio as redis  # local import — optional
    except ImportError:
        return
    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    if not url:
        return
    client = redis.from_url(url)
    try:
        await client.publish("nexus:audit:mcp", json.dumps(record))
    finally:
        await client.close()


async def _record_metrics(record: dict[str, Any]) -> None:
    """Write lightweight Redis counters used by `nexus hub status` (#3784).

    - ``nexus:hub:qps:<epoch-minute>``: INCR per audited request (10 min TTL).
    - ``nexus:hub:active:<epoch-minute>``: SADD subject_id (10 min TTL).

    Fire-and-forget: errors are swallowed so audit stays on the happy path.
    """
    try:
        import redis.asyncio as redis  # local import — optional
    except ImportError:
        return
    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    if not url:
        return
    client = redis.from_url(url)
    try:
        epoch_min = int(time.time()) // 60
        qps_key = f"nexus:hub:qps:{epoch_min}"
        active_key = f"nexus:hub:active:{epoch_min}"
        await client.incr(qps_key)
        await client.expire(qps_key, 600)
        member = record.get("subject_id") or record.get("token_hash") or "anonymous"
        await client.sadd(active_key, member)
        await client.expire(active_key, 600)
    except Exception:  # noqa: BLE001 — fire-and-forget
        return
    finally:
        await client.close()


def _hash_token(auth_header: str) -> str | None:
    lowered = auth_header.lower()
    if not lowered.startswith("bearer "):
        return None
    token = auth_header[7:]  # preserves original case of the token
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _extract_rpc_fields(body_bytes: bytes) -> tuple[str | None, str | None]:
    """Return (rpc_method, tool_name) by peeking at the JSON-RPC body."""
    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    rpc_method = payload.get("method") if isinstance(payload.get("method"), str) else None
    tool_name: str | None = None
    params = payload.get("params")
    if isinstance(params, dict):
        name = params.get("name")
        if isinstance(name, str):
            tool_name = name
    return rpc_method, tool_name


class MCPAuditLogMiddleware:
    """Pure ASGI middleware — buffers body, forwards, emits audit record."""

    _pending_tasks: set[Any] = set()  # class-level; retains task references mid-flight

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()

        # Buffer request body
        body_chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                # Client gone before we ever got a response
                self._record_from_scope(scope, start=start, status=499)
                return
        body = b"".join(body_chunks)

        # Build a receive that replays the buffered body once, then forwards
        # subsequent receives to the real transport (so real http.disconnect
        # events still reach the app, e.g. for SSE streaming where the app
        # may poll receive() to detect client aborts mid-response).
        sent = {"done": False}

        async def wrapped_receive() -> Message:
            if not sent["done"]:
                sent["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        # Capture the response status
        status_holder = {"code": 500}

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        rpc_method, tool_name = _extract_rpc_fields(body)
        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        finally:
            self._record_from_scope(
                scope,
                start=start,
                status=status_holder["code"],
                rpc_method=rpc_method,
                tool_name=tool_name,
            )

    def _record_from_scope(
        self,
        scope: Scope,
        *,
        start: float,
        status: int,
        rpc_method: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        token_hash = _hash_token(auth)
        user_agent = headers.get("user-agent", "")
        identity = scope.get("nexus.identity") or {}

        # If scope["nexus.identity"] wasn't populated by an upstream middleware,
        # fall back to AuthIdentityCache by token hash. The first tool call
        # for a token populates the cache; subsequent calls find zone/subject
        # here even without explicit scope threading.
        zone_id = identity.get("zone_id")
        subject_id = identity.get("subject_id")
        if zone_id is None and auth.lower().startswith("bearer "):
            try:
                from nexus.bricks.mcp.auth_cache import (
                    get_auth_identity_cache,
                    hash_api_key,
                )

                cached = get_auth_identity_cache().get(hash_api_key(auth[7:]))
                if cached is not None:
                    zone_id = cached.zone_id
                    subject_id = cached.subject_id
            except Exception:
                pass

        record = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "event": "mcp.request",
            "token_hash": token_hash,
            "zone_id": zone_id,
            "subject_id": subject_id,
            "rpc_method": rpc_method,
            "tool_name": tool_name,
            "status_code": status,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "user_agent": user_agent,
        }
        try:
            _emit_stdout_record(record)
        except Exception:
            logger.warning("audit stdout emit failed", exc_info=True)

        # Schedule fire-and-forget publish and RETAIN the task reference
        # so it isn't garbage-collected mid-flight (asyncio docs warn about this).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._safe_publish(record))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    @staticmethod
    async def _safe_publish(record: dict[str, Any]) -> None:
        try:
            await _publish_record(record)
        except Exception:
            logger.warning("mcp audit publish failed", exc_info=True)
        try:
            await _record_metrics(record)
        except Exception:
            logger.warning("mcp audit metrics failed", exc_info=True)


__all__ = ["MCPAuditLogMiddleware"]

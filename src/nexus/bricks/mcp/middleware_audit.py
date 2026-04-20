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

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.types import Message

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


def _hash_token(auth_header: str) -> str | None:
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
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


async def _read_and_replay_body(request: Request) -> bytes:
    """Read the request body once; rewire `scope["receive"]` to replay it."""
    body = await request.body()
    replayed = {"called": False}

    async def receive() -> Message:
        if not replayed["called"]:
            replayed["called"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    # Rewire the receive callable so downstream handlers can still read the body.
    request._receive = receive
    return body


class MCPAuditLogMiddleware(BaseHTTPMiddleware):
    """Emit a structured record per request; fail-safe for downstream."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        start = time.monotonic()
        token_hash = _hash_token(request.headers.get("Authorization", ""))
        user_agent = request.headers.get("User-Agent", "")
        rpc_method: str | None = None
        tool_name: str | None = None

        try:
            body = await _read_and_replay_body(request)
            rpc_method, tool_name = _extract_rpc_fields(body)
        except ClientDisconnect:
            self._record(
                status=499,
                start=start,
                token_hash=token_hash,
                rpc_method=None,
                tool_name=None,
                user_agent=user_agent,
                zone_id=None,
                subject_id=None,
            )
            raise
        except Exception:  # defensive: body read failure must not drop the request
            logger.warning("audit body peek failed", exc_info=True)

        status: int
        zone_id: str | None = None
        subject_id: str | None = None
        response: Response | None = None
        try:
            response = await call_next(request)
            status = response.status_code
        except ClientDisconnect:
            status = 499
        else:
            # best-effort: identity fields populated by downstream handler via scope
            scope_state = request.scope.get("nexus.identity") or {}
            zone_id = scope_state.get("zone_id")
            subject_id = scope_state.get("subject_id")

        self._record(
            status=status,
            start=start,
            token_hash=token_hash,
            rpc_method=rpc_method,
            tool_name=tool_name,
            user_agent=user_agent,
            zone_id=zone_id,
            subject_id=subject_id,
        )
        if response is None:
            raise ClientDisconnect()
        return response

    def _record(
        self,
        *,
        status: int,
        start: float,
        token_hash: str | None,
        rpc_method: str | None,
        tool_name: str | None,
        user_agent: str,
        zone_id: str | None,
        subject_id: str | None,
    ) -> None:
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
        except Exception:  # pragma: no cover - stdout is resilient
            logger.warning("audit stdout emit failed", exc_info=True)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._safe_publish(record))
        except RuntimeError:
            # No running loop (shouldn't happen under ASGI); skip publish.
            pass

    @staticmethod
    async def _safe_publish(record: dict[str, Any]) -> None:
        try:
            await _publish_record(record)
        except Exception:
            logger.warning("mcp audit publish failed", exc_info=True)


__all__ = ["MCPAuditLogMiddleware"]

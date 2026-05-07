"""MCP HTTP client helpers for remote `nexus hub` administration."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx


class HubRemoteError(Exception):
    """Raised for user-facing remote hub administration failures."""


def normalize_mcp_url(remote: str) -> str:
    """Normalize a hub base URL to its MCP streamable HTTP endpoint."""
    parsed = urlparse(remote)
    if not parsed.scheme or not parsed.netloc:
        raise HubRemoteError(f"invalid remote URL: {remote}")

    path = parsed.path.rstrip("/")
    if path in ("", "/"):
        path = "/mcp"
    elif path != "/mcp":
        path = f"{path}/mcp"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def call_hub_admin_tool(
    remote: str,
    admin_token: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Call one hub admin MCP tool and return its JSON object payload."""
    url = normalize_mcp_url(remote)
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            session_id = _initialize(client, url, headers)
            session_headers = {**headers, "Mcp-Session-Id": session_id}
            _notify_initialized(client, url, session_headers)
            envelope = _post_json_rpc(
                client,
                url,
                session_headers,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
            )
    except httpx.HTTPStatusError as exc:
        raise HubRemoteError(
            f"remote hub request failed: {exc.response.status_code} {exc.response.reason_phrase}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HubRemoteError(f"remote hub request failed: {exc}") from exc

    return _extract_tool_payload(envelope)


def _initialize(client: httpx.Client, url: str, headers: dict[str, str]) -> str:
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "nexus-hub-cli", "version": "1"},
        },
    }
    with client.stream("POST", url, headers=headers, json=envelope) as response:
        response.raise_for_status()
        session_id_raw = response.headers.get("mcp-session-id") or response.headers.get(
            "Mcp-Session-Id"
        )
        for _line in response.iter_lines():
            pass
    if not isinstance(session_id_raw, str) or not session_id_raw:
        raise HubRemoteError("remote hub MCP initialize did not return a session id")
    return session_id_raw


def _notify_initialized(client: httpx.Client, url: str, headers: dict[str, str]) -> None:
    response = client.post(
        url,
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    response.raise_for_status()


def _post_json_rpc(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    envelope: dict[str, Any],
) -> dict[str, Any]:
    with client.stream("POST", url, headers=headers, json=envelope) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                return _decode_sse_json(line)
    raise HubRemoteError("remote hub MCP response did not include a data event")


def _decode_sse_json(line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line[6:])
    except json.JSONDecodeError as exc:
        raise HubRemoteError("remote hub MCP response contained invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HubRemoteError("remote hub MCP response was not a JSON object")
    return payload


def _extract_tool_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    if envelope.get("error") is not None:
        error = envelope["error"]
        message = (error.get("message") or str(error)) if isinstance(error, dict) else str(error)
        raise HubRemoteError(message)

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise HubRemoteError("remote hub MCP response did not include a tool result")

    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise HubRemoteError("remote hub MCP tool result did not include content")

    first = content[0]
    if not isinstance(first, dict):
        raise HubRemoteError("remote hub MCP tool content was malformed")

    text = first.get("text")
    if not isinstance(text, str):
        raise HubRemoteError("remote hub MCP tool content was not text")
    if text.startswith("Error:"):
        raise HubRemoteError(text.removeprefix("Error:").strip())

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HubRemoteError("remote hub MCP tool returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HubRemoteError("remote hub MCP tool returned non-object JSON")
    return payload

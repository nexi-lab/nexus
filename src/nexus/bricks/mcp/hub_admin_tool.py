"""MCP hub-admin tool registration (#3872)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastmcp import Context

from nexus.contracts.exceptions import NexusError, NexusPermissionError


def _json_error(status: int, message: str) -> str:
    return json.dumps({"error": {"status": status, "message": message}}, indent=2)


def _admin_service(nx_instance: Any) -> Any:
    if not hasattr(nx_instance, "service"):
        raise RuntimeError("Nexus service registry is unavailable")
    service = nx_instance.service("mcp")
    if service is None:
        raise RuntimeError("Remote admin service is unavailable")
    return service


def register_hub_admin_tool(
    mcp: Any,
    get_nexus_instance: Callable[[Context | None], Any],
) -> None:
    """Register the hub admin MCP tool on a FastMCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    async def nexus_hub_admin(
        action: str,
        arguments: dict[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Administer Nexus hub tokens. Requires an admin bearer token."""
        args = arguments or {}
        nx_instance = get_nexus_instance(ctx)
        service = _admin_service(nx_instance)
        try:
            if action == "create_token":
                result = service.admin_hub_token_create(**args)
            elif action == "list_tokens":
                result = service.admin_hub_token_list(**args)
            elif action == "revoke_token":
                result = service.admin_hub_token_revoke(**args)
            elif action == "status":
                result = service.admin_hub_status(**args)
            else:
                return _json_error(400, f"unknown hub admin action: {action}")
            return json.dumps(result, indent=2, default=str)
        except AttributeError as exc:
            return _json_error(501, f"hub admin action unavailable: {exc}")
        except NexusPermissionError as exc:
            return _json_error(403, str(exc))
        except NexusError as exc:
            return _json_error(exc.status_code, str(exc))
        except FileNotFoundError as exc:
            return _json_error(404, str(exc))
        except ValueError as exc:
            return _json_error(400, str(exc))

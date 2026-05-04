"""Hub admin RPC handlers used by public MCP admin tools."""

from __future__ import annotations

from typing import Any

from nexus.hub import admin_ops
from nexus.server.rpc.handlers.admin import require_admin, require_database_auth


def _session_factory(auth_provider: Any) -> Any:
    require_database_auth(auth_provider)
    return auth_provider.session_factory


def handle_hub_admin_token_create(
    auth_provider: Any,
    params: Any,
    context: Any,
) -> dict[str, Any]:
    """Create a hub token through the shared DB-backed operation."""
    require_admin(context)
    return admin_ops.create_hub_token(
        _session_factory(auth_provider),
        name=params.name,
        zones_csv=getattr(params, "zones", None),
        zones_glob=getattr(params, "zones_glob", None),
        is_admin=bool(getattr(params, "admin", False)),
        expires=getattr(params, "expires", None),
        user_id=getattr(params, "user_id", None),
    )


def handle_hub_admin_token_list(
    auth_provider: Any,
    params: Any,
    context: Any,
) -> dict[str, Any]:
    """List hub tokens through the shared DB-backed operation."""
    require_admin(context)
    return admin_ops.list_hub_tokens(
        _session_factory(auth_provider),
        show_revoked=bool(getattr(params, "show_revoked", False)),
    )


def handle_hub_admin_token_revoke(
    auth_provider: Any,
    params: Any,
    context: Any,
) -> dict[str, Any]:
    """Revoke a hub token through the shared DB-backed operation."""
    require_admin(context)
    return admin_ops.revoke_hub_token(
        _session_factory(auth_provider),
        identifier=params.identifier,
    )


def handle_hub_admin_status(
    auth_provider: Any,
    _params: Any,
    context: Any,
) -> dict[str, Any]:
    """Read hub status through the shared DB-backed operation."""
    require_admin(context)
    return admin_ops.get_hub_status(_session_factory(auth_provider))

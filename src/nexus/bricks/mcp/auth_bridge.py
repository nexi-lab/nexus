"""MCP ↔ auth identity bridge helpers (#3731).

Resolves per-request subject identity for MCP search tools so they
can apply the same ReBAC filtering as the HTTP endpoints.

Extracted from ``server.py`` to keep that file under the 2000-line
limit enforced by pre-commit.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


def op_context_to_auth_dict(op_context: Any) -> dict[str, Any]:
    """Convert an ``OperationContext`` (or None) into an auth_result dict.

    ``_apply_rebac_filter`` expects a dict with ``subject_id``,
    ``zone_id``, and ``is_admin`` keys — the same shape that the HTTP
    ``require_auth`` dependency returns.  This helper bridges the MCP
    ``OperationContext`` into that format.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if op_context is None:
        return {
            "subject_id": "anonymous",
            "zone_id": ROOT_ZONE_ID,
            "is_admin": False,
        }
    return {
        "subject_id": getattr(op_context, "subject_id", None)
        or getattr(op_context, "user_id", "anonymous"),
        "zone_id": getattr(op_context, "zone_id", None) or ROOT_ZONE_ID,
        "is_admin": bool(getattr(op_context, "is_admin", False)),
    }


def authenticate_api_key(auth_provider: Any, api_key: str) -> Any:
    """Call ``auth_provider.authenticate(api_key)`` from sync context.

    Uses ``AuthIdentityCache`` (TTL 60s) to avoid the 10s async→sync
    bridge on every MCP tool call. Only positive results are cached.
    """
    from nexus.bricks.mcp.auth_cache import (
        ResolvedIdentity,
        get_auth_identity_cache,
        hash_api_key,
    )

    cache = get_auth_identity_cache()
    key_hash = hash_api_key(api_key)

    def _resolve() -> Any:
        try:
            coro = auth_provider.authenticate(api_key)
        except Exception:
            logger.warning(
                "auth_provider.authenticate() raised synchronously; "
                "falling through to NexusFS-based identity resolution.",
                exc_info=True,
            )
            return None

        if not inspect.isawaitable(coro):
            return coro

        try:
            from collections.abc import Coroutine as CoroutineABC
            from typing import cast

            from nexus.lib.sync_bridge import run_sync

            return run_sync(cast(CoroutineABC[Any, Any, Any], coro), timeout=10.0)
        except Exception:
            logger.warning(
                "Failed to authenticate per-request API key via auth_provider; "
                "falling through to NexusFS-based identity resolution.",
                exc_info=True,
            )
            return None

    # Fast path: cache hit (via get_or_resolve to hold lock across resolve).
    from types import SimpleNamespace

    def _resolve_and_cache() -> ResolvedIdentity | None:
        auth_result = _resolve()
        if auth_result is None:
            return None
        subject_id = getattr(auth_result, "subject_id", None) or getattr(
            auth_result, "user_id", None
        )
        zone_id = getattr(auth_result, "zone_id", None)
        is_admin = bool(getattr(auth_result, "is_admin", False))
        # Default authenticated=True when absent: a non-None return from the provider
        # implies success (matches original fail-closed behavior where the check was implicit).
        authenticated = bool(getattr(auth_result, "authenticated", True))
        if not authenticated or not subject_id or not zone_id:
            return None
        return ResolvedIdentity(
            subject_id=subject_id,
            zone_id=zone_id,
            is_admin=is_admin,
            tier="premium" if is_admin else "authenticated",
            subject_type=getattr(auth_result, "subject_type", None) or "user",
            agent_generation=getattr(auth_result, "agent_generation", None),
            inherit_permissions=getattr(auth_result, "inherit_permissions", None),
        )

    resolved = cache.get_or_resolve(key_hash, _resolve_and_cache)
    if resolved is None:
        return None

    return SimpleNamespace(
        authenticated=True,
        subject_type=resolved.subject_type,
        subject_id=resolved.subject_id,
        zone_id=resolved.zone_id,
        is_admin=resolved.is_admin,
        agent_generation=resolved.agent_generation,
        inherit_permissions=resolved.inherit_permissions,
    )


def resolve_mcp_operation_context(
    nx_instance: NexusFS,
    auth_provider: Any | None = None,
) -> Any:
    """Resolve an explicit ``OperationContext`` for MCP search calls.

    Resolution priority (first non-None wins):

    0. Per-request API key via ``_request_api_key`` contextvar +
       ``auth_provider`` (#3731).
    1. ``nx_instance._init_cred`` — kernel process credential.
    2. ``nx_instance._default_context`` — legacy fallback.
    3. Remote-connection whoami cache (``subject_id`` etc.).
    4. ``None`` — let SearchService use its own default.
    """
    from nexus.bricks.mcp.server import _request_api_key
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext

    # (0) Per-request API key — most authoritative when MCP is behind
    # HTTP middleware that sets _request_api_key (#3731).
    #
    # When auth_provider is available, use it to verify the key.
    # If verification fails, fail closed (return None).
    #
    # When auth_provider is NOT available but a per-request key is set,
    # _get_nexus_instance already created a remote NexusFS scoped to
    # that key — its _init_cred IS the per-request identity (not
    # ambient). So we allow steps 1-3 to proceed.
    request_key = _request_api_key.get()
    if request_key and auth_provider is None:
        # Per-request key set but no auth_provider to verify it.
        # Fall through to NexusFS-based identity (steps 1-3).
        # In remote mode, _get_nexus_instance already created a
        # connection scoped to this key — _init_cred is that identity.
        # In local mode, _init_cred is the process identity (single
        # user, no multi-tenancy concern).
        #
        # NOTE: callers (e.g. CLI) should thread auth_provider for
        # full verification. This fallback is safe but less strict.
        logger.info(
            "Per-request API key set but no auth_provider available; "
            "using NexusFS-based identity (steps 1-3)."
        )
    if request_key and auth_provider is not None:
        auth_result = authenticate_api_key(auth_provider, request_key)
        if auth_result is not None:
            # Normalize to dict so we can use the shared HTTP helper.
            if hasattr(auth_result, "__dataclass_fields__"):
                import dataclasses

                auth_dict = dataclasses.asdict(auth_result)
            elif hasattr(auth_result, "get"):
                auth_dict = dict(auth_result)
            else:
                auth_dict = {
                    k: getattr(auth_result, k, None)
                    for k in (
                        "authenticated",
                        "subject_type",
                        "subject_id",
                        "zone_id",
                        "is_admin",
                        "agent_generation",
                        "inherit_permissions",
                    )
                }

            if auth_dict.get("authenticated", False):
                subject_type = auth_dict.get("subject_type") or "user"
                subject_id = auth_dict.get("subject_id") or "anonymous"
                zone_id = auth_dict.get("zone_id") or ROOT_ZONE_ID
                is_admin = bool(auth_dict.get("is_admin", False))
                agent_generation = auth_dict.get("agent_generation")
                return OperationContext(
                    user_id=subject_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    zone_id=zone_id,
                    groups=[],
                    is_admin=is_admin,
                    is_system=False,
                    agent_generation=agent_generation,
                )
        # Per-request key was present and auth_provider actively
        # rejected it — fail closed. Do NOT fall through to ambient
        # creds from _init_cred/_default_context.
        logger.warning(
            "Per-request API key authentication failed or was rejected; "
            "returning None (fail-closed) instead of falling through "
            "to ambient credentials."
        )
        return None

    # (1) Kernel init_cred.
    init_cred = getattr(nx_instance, "_init_cred", None)
    if init_cred is not None:
        return init_cred

    # (2) Legacy default_context.
    default_ctx = getattr(nx_instance, "_default_context", None)
    if default_ctx is not None:
        return default_ctx

    # (3) Bare remote backend whoami fields.
    subject_id = getattr(nx_instance, "subject_id", None)
    if subject_id:
        subject_type = getattr(nx_instance, "subject_type", None) or "user"
        zone_id = getattr(nx_instance, "zone_id", None) or ROOT_ZONE_ID
        is_admin = bool(getattr(nx_instance, "is_admin", False))
        return OperationContext(
            user_id=subject_id,
            subject_type=subject_type,
            subject_id=subject_id,
            zone_id=zone_id,
            groups=[],
            is_admin=is_admin,
            is_system=False,
        )

    # (4) Last resort.
    logger.warning(
        "MCP search tool could not resolve an explicit OperationContext "
        "from the NexusFS (no _init_cred, no _default_context, "
        "no whoami identity). Falling back to SearchService's default "
        "context — the server-side auth layer remains the source of truth."
    )
    return None

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

    ``authenticate()`` is always async in the Nexus auth provider
    contract.  This helper handles the async-to-sync bridge regardless
    of whether an event loop is already running (MCP grep is async,
    MCP glob is sync).

    Returns the ``AuthResult`` on success, ``None`` on any failure.
    """
    import asyncio
    import concurrent.futures

    try:
        coro = auth_provider.authenticate(api_key)
    except Exception:
        logger.warning(
            "auth_provider.authenticate() raised synchronously; "
            "falling through to NexusFS-based identity resolution.",
            exc_info=True,
        )
        return None

    # If not actually a coroutine (mock / sync provider), return directly.
    if not inspect.isawaitable(coro):
        return coro

    # Use a background thread so we work whether or not an event loop
    # is already running (same pattern as _get_nexus_instance).
    def _run() -> Any:
        return asyncio.run(coro)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run).result(timeout=10)
    except Exception:
        logger.warning(
            "Failed to authenticate per-request API key via auth_provider; "
            "falling through to NexusFS-based identity resolution.",
            exc_info=True,
        )
        return None


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
    request_key = _request_api_key.get()
    if request_key and auth_provider is not None:
        auth_result = authenticate_api_key(auth_provider, request_key)
        if auth_result is not None:
            _get = (
                auth_result.get
                if hasattr(auth_result, "get")
                else lambda k, d=None: getattr(auth_result, k, d)
            )
            if _get("authenticated", False):
                subject_id = _get("subject_id", None) or "anonymous"
                zone_id = _get("zone_id", None) or ROOT_ZONE_ID
                is_admin = bool(_get("is_admin", False))
                subject_type = _get("subject_type", None) or "user"
                return OperationContext(
                    user_id=subject_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    zone_id=zone_id,
                    groups=[],
                    is_admin=is_admin,
                    is_system=False,
                )

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

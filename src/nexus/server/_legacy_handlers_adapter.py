"""Adapter that exposes the residual non-@rpc_expose handlers as
methods on a single service class so the Rust ``python_ffi`` router
can route the wire RPCs to them.

Why this exists: the legacy ``handlers/admin.py`` /
``handlers/delta.py`` / ``handlers/filesystem.py`` (search-utils
subset) functions take a positional ``(nexus_fs/auth_provider,
params, context)`` signature, which the python_ffi router (which
expects ``method(**kwargs)`` on a service instance) can't call
directly.  Wrapping them in a class with kwarg-style methods
unblocks registration and lets the legacy ``dispatch.py`` table +
``server.rpc.handlers`` package go away in #45.

Once the underlying handlers move to Rust services (or get
@rpc_expose'd on a proper service class), this adapter becomes
redundant and can be deleted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class LegacyHandlersAdapter:
    """Single instance registered with python_ffi at boot.

    Holds references to ``nexus_fs`` (for filesystem handlers) and
    ``auth_provider`` (for admin handlers).  Each method translates
    kwargs → ``SimpleNamespace`` params + delegates to the original
    handler function.
    """

    def __init__(
        self,
        nexus_fs: Any,
        auth_provider: Any,
    ) -> None:
        self._nexus_fs = nexus_fs
        self._auth_provider = auth_provider

    @staticmethod
    def _params(**kwargs: Any) -> tuple[SimpleNamespace, Any]:
        """Split kwargs into ``(params, context)`` matching the legacy handler shape."""
        ctx = kwargs.pop("context", None)
        return SimpleNamespace(**kwargs), ctx

    # ── delta sync ───────────────────────────────────────────────

    async def delta_read(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.delta import handle_delta_read

        params, context = self._params(**kwargs)
        return await handle_delta_read(self._nexus_fs, params, context)

    async def delta_write(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.delta import handle_delta_write

        params, context = self._params(**kwargs)
        return await handle_delta_write(self._nexus_fs, params, context)

    # ── filesystem utilities (non-syscall) ───────────────────────

    def copy(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.filesystem import handle_copy

        params, context = self._params(**kwargs)
        return handle_copy(self._nexus_fs, params, context)

    def glob(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.filesystem import handle_glob

        params, context = self._params(**kwargs)
        return handle_glob(self._nexus_fs, params, context)

    async def grep(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.filesystem import handle_grep

        params, context = self._params(**kwargs)
        return await handle_grep(self._nexus_fs, params, context)

    def search(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.filesystem import handle_search

        params, context = self._params(**kwargs)
        return handle_search(self._nexus_fs, params, context)

    async def semantic_search(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.filesystem import handle_semantic_search

        params, context = self._params(**kwargs)
        return await handle_semantic_search(self._nexus_fs, params, context)

    async def semantic_search_index(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.filesystem import handle_semantic_search_index

        params, context = self._params(**kwargs)
        return await handle_semantic_search_index(self._nexus_fs, params, context)

    async def ainitialize_semantic_search(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.filesystem import handle_ainitialize_semantic_search

        params, context = self._params(**kwargs)
        return await handle_ainitialize_semantic_search(self._nexus_fs, params, context)

    # ── admin / auth-provider-bound ──────────────────────────────

    def admin_write_permission(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.admin import handle_admin_write_permission

        params, context = self._params(**kwargs)
        return handle_admin_write_permission(self._nexus_fs, params, context)

    def admin_create_key(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.admin import handle_admin_create_key

        params, context = self._params(**kwargs)
        return handle_admin_create_key(self._auth_provider, params, context)

    def admin_list_keys(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.admin import handle_admin_list_keys

        params, context = self._params(**kwargs)
        return handle_admin_list_keys(self._auth_provider, params, context)

    def admin_get_key(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.admin import handle_admin_get_key

        params, context = self._params(**kwargs)
        return handle_admin_get_key(self._auth_provider, params, context)

    def admin_revoke_key(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.admin import handle_admin_revoke_key

        params, context = self._params(**kwargs)
        return handle_admin_revoke_key(self._auth_provider, params, context)

    def admin_update_key(self, **kwargs: Any) -> dict[str, Any]:
        from nexus.server.rpc.handlers.admin import handle_admin_update_key

        params, context = self._params(**kwargs)
        return handle_admin_update_key(self._auth_provider, params, context)


# Wire-form RPC name list for python_ffi registration.  Order has no
# semantic meaning — kept alphabetical for readability.
LEGACY_HANDLER_WIRE_NAMES: tuple[str, ...] = (
    "ainitialize_semantic_search",
    "admin_create_key",
    "admin_get_key",
    "admin_list_keys",
    "admin_revoke_key",
    "admin_update_key",
    "admin_write_permission",
    "copy",
    "delta_read",
    "delta_write",
    "glob",
    "grep",
    "search",
    "semantic_search",
    "semantic_search_index",
)

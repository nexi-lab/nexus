"""InternalMixin — shared helpers used by Content/Metadata/Dispatch mixins.

Extracts cross-cutting utility methods that are called from multiple VFS
domains (read, write, stat, rename, …). Having them in a dedicated mixin
decouples the large ContentMixin and MetadataMixin from each other — both
depend on InternalMixin via MRO rather than on each other directly.

Mixin rules (Phase 6 established):
  • ``from __future__ import annotations`` + TYPE_CHECKING stubs
  • Single stub: ``_kernel: Any`` — other NexusFS attrs accessed via MRO
  • Listed BEFORE NexusFilesystemABC in MRO
  • No new ``type: ignore``
"""

from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING, Any

from nexus.contracts.types import OperationContext
from nexus.core.path_utils import validate_path

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class InternalMixin:
    """Shared helpers for NexusFS mixins: context, validation, .readme overlay, events."""

    _kernel: Any  # Rust Kernel — single stub, other attrs via MRO
    _zone_id: str
    _init_cred: Any
    _driver_coordinator: Any

    # ── Context helpers ──────────────────────────────────────────────

    def _validate_path(self, path: str, allow_root: bool = False) -> str:
        """Validate and normalize virtual path. Delegates to lib/path_utils."""
        return validate_path(path, allow_root=allow_root)

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext."""
        from nexus.lib.context_utils import parse_context

        return parse_context(context)

    def _build_rust_ctx(self, context: "OperationContext | None", is_admin: bool) -> object:
        """Build Rust OperationContext from Python context with all fields.

        Caches the built Rust ctx on the Python OperationContext instance so
        repeat calls within the same syscall chain skip 11 string field copies
        across the PyO3 boundary.  Cache key is ``is_admin`` — identity fields
        are immutable within a request.
        """
        if context is not None:
            cached = context.__dict__.get("_rust_ctx_cache")
            if cached is not None and cached[0] == is_admin:
                return cached[1]

        from nexus_kernel import PyOperationContext as _RustCtx

        rust_ctx = _RustCtx(
            user_id=context.user_id if context else "anonymous",
            zone_id=self._zone_id,  # routing zone (always set)
            is_admin=is_admin,
            agent_id=getattr(context, "agent_id", None) if context else None,
            is_system=getattr(context, "is_system", False) if context else False,
            groups=context.groups if context else [],
            admin_capabilities=list(context.admin_capabilities) if context else [],
            subject_type=getattr(context, "subject_type", "user") if context else "user",
            subject_id=getattr(context, "subject_id", None) if context else None,
            request_id=getattr(context, "request_id", "") if context else "",
            context_zone_id=context.zone_id if context else None,  # caller's zone
        )

        if context is not None:
            context.__dict__["_rust_ctx_cache"] = (is_admin, rust_ctx)

        return rust_ctx

    def _get_context_identity(
        self, context: OperationContext | dict | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract (zone_id, agent_id, is_admin) from context."""
        if context is None:
            ctx = self._resolve_cred(None)
            return (ctx.zone_id, ctx.agent_id, ctx.is_admin)
        if isinstance(context, dict):
            fallback = self._resolve_cred(None)
            return (
                context.get("zone_id", fallback.zone_id),
                context.get("agent_id", fallback.agent_id),
                context.get("is_admin", fallback.is_admin),
            )
        return context.zone_id, context.agent_id, getattr(context, "is_admin", False)

    def _resolve_cred(self, context: OperationContext | None) -> OperationContext:
        """Return *context* or the kernel init_cred; raise if neither available.

        Issue #1801: kernel never fabricates identity — like Linux VFS,
        every syscall requires credentials from the caller.  Renamed from
        ``_require_context`` to reflect its role: resolve the credential
        chain (explicit → init_cred → error).
        """
        if context is not None:
            return context
        if self._init_cred is not None:
            return self._init_cred
        raise ValueError(
            "No operation context provided and no init_cred configured. "
            "Use factory create_nexus_fs(init_cred=...) or pass context= to each syscall."
        )

    def _ensure_context_ttl(self, context: OperationContext | None, ttl: float) -> OperationContext:
        """Ensure context exists and has ttl_seconds set (Issue #3405)."""
        if context is not None:
            return _dc_replace(context, ttl_seconds=ttl)
        return OperationContext(user_id="anonymous", groups=[], ttl_seconds=ttl)

    # ── Batch permission check ─────────────────────────────────────

    def _batch_permission_check(
        self,
        paths: list[str],
        context: "OperationContext | None",
        permission: str = "READ",
    ) -> set[str]:
        """Return the set of *paths* that pass stat permission hooks.

        Uses a single FFI call to Rust ``dispatch_pre_hooks_batch_stat``
        which loops N paths internally — avoiding N PyO3 boundary crossings.
        Fast path: no hooks → all paths allowed (checked in Rust).
        """
        ctx = self._resolve_cred(context)
        is_admin = getattr(ctx, "is_admin", False)
        rust_ctx = self._build_rust_ctx(ctx, is_admin)
        results = self._kernel.dispatch_pre_hooks_batch_stat(paths, rust_ctx, permission)
        return {p for p, allowed in zip(paths, results, strict=True) if allowed}

    # _dispatch_write_events deleted — callers inline the post-hook dispatch.

"""Virtual view resolver — VFSPathResolver for parsed virtual paths (#332, #889).

Implements the ``VFSPathResolver`` protocol and is registered in
``KernelDispatch`` as a PRE-DISPATCH resolver.  When a read targets a
virtual parsed path (e.g., ``report_parsed.pdf.md``), this handler
short-circuits the normal VFS pipeline and handles the operation directly.

Linux analogue: overlay-like read transformation — the resolver reads
the original file from the underlying filesystem and returns a parsed
(transformed) view of it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.types import Permission
from nexus.contracts.vfs_hooks import VFSPathResolver

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec

logger = logging.getLogger(__name__)


class VirtualViewResolver(VFSPathResolver):
    """PRE-DISPATCH resolver for virtual parsed view paths.

    Implements ``VFSPathResolver`` protocol:
    - ``matches(path)`` — routing predicate
    - ``read(path, ...)`` — read original file + parse to markdown
    - ``write(path, content)`` — always raises (read-only)
    - ``delete(path, ...)`` — always raises (read-only)

    Dependencies injected via constructor:
    - metadata: MetastoreABC (file existence + metadata lookup)
    - path_router: PathRouter (CAS content routing)
    - permission_checker: PermissionChecker (read permission verification)
    - parse_fn: Optional callable for content parsing
    - read_tracker_fn: Optional callable for dependency tracking (#1166)
    """

    __slots__ = (
        "_metadata",
        "_path_router",
        "_permission_checker",
        "_parse_fn",
        "_read_tracker_fn",
    )

    # ── HotSwappable protocol (Issue #1612) ────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(resolvers=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    def __init__(
        self,
        metadata: Any,
        path_router: Any,
        permission_checker: Any,
        parse_fn: Any = None,
        read_tracker_fn: Any = None,
    ) -> None:
        self._metadata = metadata
        self._path_router = path_router
        self._permission_checker = permission_checker
        self._parse_fn = parse_fn
        self._read_tracker_fn = read_tracker_fn

    # ------------------------------------------------------------------
    # VFSPathResolver protocol
    # ------------------------------------------------------------------

    def matches(self, path: str) -> bool:
        """Return True if *path* is a virtual parsed view."""
        from nexus.lib.virtual_views import parse_virtual_path

        _, view_type = parse_virtual_path(path, self._metadata.exists)
        return view_type == "md"

    def read(
        self, path: str, *, return_metadata: bool = False, context: Any = None
    ) -> bytes | dict[str, Any]:
        """Read virtual parsed view."""
        from nexus.lib.virtual_views import get_parsed_content, parse_virtual_path

        original_path, view_type = parse_virtual_path(path, self._metadata.exists)
        if view_type != "md":
            raise NexusFileNotFoundError(f"Not a virtual view: {path}")

        # Permission check — resolver owns its permission semantics.
        # The checker resolves virtual paths to the original file internally.
        self._permission_checker.check(path, Permission.READ, context)

        logger.info("read: Virtual view detected, reading original file: %s", original_path)

        # Route and read original file content
        is_admin = bool(getattr(context, "is_admin", False)) if context else False
        route = self._path_router.route(original_path, is_admin=is_admin, check_write=False)
        meta = self._metadata.get(original_path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(original_path)

        # Add backend_path to context for path-based connectors
        read_context = context
        if context:
            from dataclasses import replace

            read_context = replace(context, backend_path=route.backend_path)

        content: bytes = route.backend.read_content(meta.etag, context=read_context)

        # Parse content to markdown
        content = get_parsed_content(
            content,
            original_path,
            view_type,
            parse_fn=self._parse_fn,
        )

        # Issue #1166: Record read for dependency tracking (virtual view reads original file)
        if self._read_tracker_fn is not None:
            self._read_tracker_fn(context, "file", original_path, "content")

        if return_metadata:
            return {
                "content": content,
                "etag": meta.etag + ".md",  # Synthetic etag for virtual view
                "version": meta.version,
                "modified_at": meta.modified_at,
                "size": len(content),
            }
        return content

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        """Virtual views are read-only."""
        raise NexusFileNotFoundError(f"Cannot write to virtual view: {path} ({len(content)} bytes)")

    def delete(self, path: str, *, context: Any = None) -> None:
        """Virtual views are read-only."""
        _ = context  # Required by VFSPathResolver protocol
        raise NexusFileNotFoundError(f"Cannot delete virtual view: {path}")

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

    Implements ``VFSPathResolver`` single-call ``try_*`` protocol (#1665):
    - ``try_read(path, ...)`` — check if virtual view, read + parse to markdown
    - ``try_write(path, content)`` — raises if virtual view, else returns None
    - ``try_delete(path, ...)`` — raises if virtual view, else returns None

    Dependencies injected via constructor:
    - metadata: MetastoreABC (file existence + metadata lookup)
    - kernel: Rust kernel (VFS routing)
    - dlc: DriverLifecycleCoordinator (backend refs)
    - permission_checker: PermissionChecker (read permission verification)
    - parse_fn: Optional callable for content parsing
    - read_tracker_fn: Optional callable for dependency tracking (#1166)
    """

    __slots__ = (
        "_metadata",
        "_kernel",
        "_dlc",
        "_permission_checker",
        "_parse_fn",
        "_read_tracker_fn",
    )

    # ── Hook spec (duck-typed) (Issue #1612) ──────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(resolvers=(self,))

    def __init__(
        self,
        metadata: Any,
        dlc: Any = None,
        permission_checker: Any = None,
        parse_fn: Any = None,
        read_tracker_fn: Any = None,
    ) -> None:
        self._metadata = metadata
        self._dlc = dlc
        self._permission_checker = permission_checker
        self._parse_fn = parse_fn
        self._read_tracker_fn = read_tracker_fn

    # ------------------------------------------------------------------
    # VFSPathResolver single-call try_* protocol (#1665)
    # ------------------------------------------------------------------

    def try_read(self, path: str, *, context: Any = None) -> bytes | None:
        """Read virtual parsed view, or return None if not a virtual view."""
        from nexus.lib.virtual_views import get_parsed_content, parse_virtual_path

        # Single metastore lookup: metadata.get returns FileMetadata (truthy)
        # or None (falsy). parse_virtual_path passes the result through.
        original_path, view_type, meta = parse_virtual_path(path, self._metadata.get)
        if view_type != "md":
            return None

        # Permission check — resolver owns its permission semantics.
        self._permission_checker.check(path, Permission.READ, context)

        logger.info("read: Virtual view detected, reading original file: %s", original_path)

        # Route and read original file content
        if meta is None or meta.content_id is None:
            raise NexusFileNotFoundError(original_path)

        # Read content via kernel syscall — sys_read_raw raises on missing path.
        _kernel = getattr(self._dlc, "_kernel", None) if self._dlc else None
        if _kernel is None:
            raise NexusFileNotFoundError(original_path)
        content: bytes = _kernel.sys_read_raw(original_path, "root")

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

        return content

    def try_write(self, path: str, content: bytes, *, context: Any = None) -> dict[str, Any] | None:
        """Virtual views are read-only — raise if virtual view, else return None."""
        _ = context
        from nexus.lib.virtual_views import parse_virtual_path

        _, view_type, _ = parse_virtual_path(path, self._metadata.exists)
        if view_type == "md":
            raise NexusFileNotFoundError(
                f"Cannot write to virtual view: {path} ({len(content)} bytes)"
            )
        return None

    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        """Virtual views are read-only — raise if virtual view, else return None."""
        from nexus.lib.virtual_views import parse_virtual_path

        _ = context
        _, view_type, _ = parse_virtual_path(path, self._metadata.exists)
        if view_type == "md":
            raise NexusFileNotFoundError(f"Cannot delete virtual view: {path}")
        return None

"""ReadmePathResolver — VFSPathResolver for /<mount>/.readme/** virtual overlay.

Intercepts reads to connector doc paths and generates content on-demand from
ReadmeDocMixin connector metadata. No disk storage — content is assembled live.

Virtual paths served:
    /<mount>/.readme/README.md           → backend.generate_readme(mount_point)
    /<mount>/.readme/schemas/<op>.yaml   → generator.generate_schema_yaml(op, schema)

Register at boot via factory._enlist("readme_resolver", resolver).
Register a backend via resolver.register_backend(mount_point, backend) after mount.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec


class ReadmePathResolver:
    """PRE-DISPATCH resolver for /<mount>/.readme/** virtual doc overlay.

    Implements VFSPathResolver single-call try_* protocol (#1665):
    - try_read: generate README.md or schemas/<op>.yaml on demand
    - try_list: enumerate the virtual .readme/ tree for sys_readdir
    - try_write / try_delete: raise PermissionError (overlay is read-only)

    Reads are gated on the caller having READ access to the owning mount —
    the overlay must not leak the existence or shape of mounts a caller
    cannot otherwise see (Issue #3827 review).
    """

    __slots__ = ("_backends", "_nx")

    def __init__(self, nexus_fs: Any | None = None) -> None:
        # mount_point (no trailing /) → ReadmeDocMixin
        self._backends: dict[str, Any] = {}
        # NexusFS handle for permission gating; None disables the gate
        # (only safe in test fixtures that drive the resolver directly).
        self._nx = nexus_fs

    # ── Backend registry ─────────────────────────────────────────────────

    def register_backend(self, mount_point: str, backend: Any) -> None:
        """Register a ReadmeDocMixin backend for a mount point."""
        self._backends[mount_point.rstrip("/")] = backend

    def unregister_backend(self, mount_point: str) -> None:
        """Remove backend registration for a mount point (noop if absent)."""
        self._backends.pop(mount_point.rstrip("/"), None)

    # ── Path matching ─────────────────────────────────────────────────────

    def _match(self, path: str) -> tuple[str, str, Any] | None:
        """Return (mount_point, rel_path, backend) if path is under .readme/."""
        for mount_point in sorted(self._backends, key=len, reverse=True):
            prefix = mount_point + "/.readme/"
            if path.startswith(prefix):
                return mount_point, path[len(prefix) :], self._backends[mount_point]
        return None

    def _match_dir(self, path: str) -> tuple[str, str, Any] | None:
        """Return (mount_point, rel_path, backend) for directory paths.

        Accepts the bare .readme directory (``rel=""``) and any subdir.
        """
        norm = path.rstrip("/")
        for mount_point in sorted(self._backends, key=len, reverse=True):
            base = mount_point + "/.readme"
            if norm == base:
                return mount_point, "", self._backends[mount_point]
            prefix = base + "/"
            if path.startswith(prefix):
                return mount_point, norm[len(prefix) :], self._backends[mount_point]
        return None

    def _caller_authorized(self, mount_point: str, context: Any) -> bool:
        """Return True if the caller has READ access on the owning mount.

        ``NexusFS.access`` only proves the path exists — the Rust kernel's
        ``sys_stat`` does not run the ReBAC chain. We instead dispatch the
        ``read`` pre-hook directly so the registered ``PermissionCheckHook``
        runs the full READ check (Issue #3827 round-3 review). When
        ``self._nx`` is unwired (unit-test fixtures), we fall open.
        """
        if self._nx is None:
            return True
        kernel = getattr(self._nx, "_kernel", None)
        if kernel is None:
            return True
        try:
            from nexus.contracts.vfs_hooks import ReadHookContext
        except Exception:
            return True
        try:
            kernel.dispatch_pre_hooks("read", ReadHookContext(path=mount_point, context=context))
        except PermissionError:
            return False
        except Exception:
            # Any non-permission failure (invalid context, missing zone) is
            # treated as deny so the overlay never widens the surface.
            return False
        return True

    # ── VFSPathResolver protocol ──────────────────────────────────────────

    def try_read(self, path: str, *, context: Any = None) -> bytes | None:
        """Return generated .readme/ content, or None if path not claimed.

        Returns None on permission denial — sys_read then falls through to
        the regular dispatch which raises NexusFileNotFoundError, matching
        the standard "no such path" surface for unauthorized callers.
        """
        match = self._match(path)
        if match is None:
            return None

        mount_point, rel, backend = match
        if not self._caller_authorized(mount_point, context):
            return None

        if rel == "README.md":
            text: str = backend.generate_readme(mount_point)
            return text.encode()

        if rel.startswith("schemas/") and rel.endswith(".yaml"):
            op_name = rel[len("schemas/") : -len(".yaml")]
            gen = backend.get_doc_generator()
            schema = gen.get_schema(op_name)
            if schema is not None:
                yaml_text: str = gen.generate_schema_yaml(op_name, schema)
                return yaml_text.encode()

        return None

    def try_list(
        self, path: str, *, context: Any = None, recursive: bool = False
    ) -> list[tuple[str, int]] | None:
        """List virtual children under ``.readme`` / ``.readme/schemas``.

        Returns ``[(absolute_path, entry_type)]`` so callers can stitch the
        result into ``sys_readdir`` with correct typing. ``entry_type``
        uses the kernel's DT enum: 0 for files (DT_REG), 1 for directories
        (DT_DIR). Honours ``recursive=True`` by emitting nested paths.
        """
        match = self._match_dir(path)
        if match is None:
            return None

        mount_point, rel, backend = match
        if not self._caller_authorized(mount_point, context):
            return None

        base = mount_point + "/.readme"
        try:
            gen = backend.get_doc_generator()
        except Exception:
            gen = None
        schemas = getattr(gen, "_schemas", None) or {}

        if rel == "":
            entries: list[tuple[str, int]] = [(base + "/README.md", 0)]
            if schemas:
                entries.append((base + "/schemas", 1))
                if recursive:
                    entries.extend((f"{base}/schemas/{op}.yaml", 0) for op in schemas)
            return entries

        if rel == "schemas":
            return [(f"{base}/schemas/{op}.yaml", 0) for op in schemas]

        return None

    def try_stat(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        """Synthesize stat for advertised .readme/ paths.

        Mirrors ``try_list`` so callers that ``readdir → stat → read`` see
        consistent metadata. Returns ``entry_type`` per the kernel DT enum
        (0=DT_REG, 1=DT_DIR). README and schema YAML stats include their
        rendered byte length so FUSE-style consumers can size buffers.
        """
        # Directory: bare ``.readme`` or ``.readme/schemas``.
        dir_match = self._match_dir(path)
        if dir_match is not None:
            mount_point, rel, backend = dir_match
            if not self._caller_authorized(mount_point, context):
                return None
            if rel == "":
                return {"path": path, "size": 0, "etag": "", "entry_type": 1}
            if rel == "schemas":
                try:
                    gen = backend.get_doc_generator()
                except Exception:
                    return None
                if not getattr(gen, "_schemas", None):
                    return None
                return {"path": path, "size": 0, "etag": "", "entry_type": 1}

        file_match = self._match(path)
        if file_match is None:
            return None
        mount_point, rel, backend = file_match
        if not self._caller_authorized(mount_point, context):
            return None

        if rel == "README.md":
            text: str = backend.generate_readme(mount_point)
            return {
                "path": path,
                "size": len(text.encode()),
                "etag": "",
                "entry_type": 0,
            }

        if rel.startswith("schemas/") and rel.endswith(".yaml"):
            op_name = rel[len("schemas/") : -len(".yaml")]
            gen = backend.get_doc_generator()
            schema = gen.get_schema(op_name)
            if schema is None:
                return None
            yaml_text: str = gen.generate_schema_yaml(op_name, schema)
            return {
                "path": path,
                "size": len(yaml_text.encode()),
                "etag": "",
                "entry_type": 0,
            }

        return None

    def try_write(self, path: str, content: bytes, *, context: Any = None) -> dict[str, Any] | None:
        """Reject writes to .readme/ overlay (read-only).

        Unauthorized callers get None — same surface as a missing path —
        so the overlay's existence is not disclosed via differential
        error messages. Authorized callers get a PermissionError that
        names the read-only contract.
        """
        _ = content
        match = self._match(path) or self._match_dir(path)
        if match is None:
            return None
        mount_point, _rel, _backend = match
        if not self._caller_authorized(mount_point, context):
            return None
        raise PermissionError(f"{path}: .readme/ overlay is read-only")

    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        """Reject deletes of .readme/ overlay (read-only).

        Same auth-aware surface as ``try_write`` — see that docstring.
        """
        match = self._match(path) or self._match_dir(path)
        if match is None:
            return None
        mount_point, _rel, _backend = match
        if not self._caller_authorized(mount_point, context):
            return None
        raise PermissionError(f"{path}: .readme/ overlay is read-only")

    # ── HookSpec ──────────────────────────────────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(resolvers=(self,))

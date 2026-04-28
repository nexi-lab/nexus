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

        When ``self._nx`` is wired we delegate to ``NexusFS.access`` which
        runs the standard ReBAC chain. Without it (test harness), we fall
        open to preserve unit-test ergonomics.
        """
        if self._nx is None:
            return True
        try:
            return bool(self._nx.access(mount_point, context=context))
        except Exception:
            # access() raises on InvalidPath etc. — treat as deny.
            return False

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

    def try_list(self, path: str, *, context: Any = None) -> list[str] | None:
        """List virtual children under `.readme` / `.readme/schemas`, or None.

        Returns absolute paths so the caller can stitch them into a
        ``sys_readdir`` result without needing to know the prefix.
        """
        match = self._match_dir(path)
        if match is None:
            return None

        mount_point, rel, backend = match
        if not self._caller_authorized(mount_point, context):
            return None

        base = mount_point + "/.readme"
        if rel == "":
            entries = [base + "/README.md"]
            try:
                gen = backend.get_doc_generator()
            except Exception:
                gen = None
            if gen is not None and getattr(gen, "_schemas", None):
                entries.append(base + "/schemas")
            return entries

        if rel == "schemas":
            try:
                gen = backend.get_doc_generator()
            except Exception:
                return []
            schemas = getattr(gen, "_schemas", None) or {}
            return [f"{base}/schemas/{op}.yaml" for op in schemas]

        return None

    def try_write(self, path: str, content: bytes) -> dict[str, Any] | None:
        """Reject writes to .readme/ overlay (read-only)."""
        _ = content
        if self._match(path) is not None:
            raise PermissionError(f"{path}: .readme/ overlay is read-only")
        return None

    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        """Reject deletes of .readme/ overlay (read-only)."""
        _ = context
        if self._match(path) is not None:
            raise PermissionError(f"{path}: .readme/ overlay is read-only")
        return None

    # ── HookSpec ──────────────────────────────────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(resolvers=(self,))

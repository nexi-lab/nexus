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
    - try_write / try_delete: raise PermissionError (overlay is read-only)
    """

    __slots__ = ("_backends",)

    def __init__(self) -> None:
        self._backends: dict[str, Any] = {}  # mount_point (no trailing /) → ReadmeDocMixin

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

    # ── VFSPathResolver protocol ──────────────────────────────────────────

    def try_read(self, path: str, *, context: Any = None) -> bytes | None:
        """Return generated .readme/ content, or None if path not claimed."""
        _ = context
        match = self._match(path)
        if match is None:
            return None

        mount_point, rel, backend = match

        if rel == "README.md":
            return backend.generate_readme(mount_point).encode()

        if rel.startswith("schemas/") and rel.endswith(".yaml"):
            op_name = rel[len("schemas/") : -len(".yaml")]
            gen = backend.get_doc_generator()
            schema = gen.get_schema(op_name)
            if schema is not None:
                return gen.generate_schema_yaml(op_name, schema).encode()

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

"""Regression coverage for sys_unlink post-hook dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_sys_unlink_dispatches_delete_post_hooks_even_when_kernel_flag_is_false() -> None:
    """Python VFS hooks are authoritative for audit/search delete propagation."""
    from nexus.core.nexus_fs_metadata import MetadataMixin

    class _Kernel:
        def __init__(self) -> None:
            self.dispatch_post_hooks = MagicMock()

        def sys_stat(self, path: str, zone_id: str) -> dict[str, object]:
            return {"path": path, "zone_id": zone_id, "content_id": "cid"}

        def sys_unlink(self, path: str, rust_ctx: object, recursive: bool) -> SimpleNamespace:
            return SimpleNamespace(
                hit=True,
                entry_type=0,
                post_hook_needed=False,
                content_id="cid",
                size=3,
            )

    class _FS(MetadataMixin):
        def __init__(self) -> None:
            self._kernel = _Kernel()
            self._hook_specs = {}

        def _prepare_rust_ctx(self, _context: object) -> tuple[str, None, bool, object]:
            return ("root", None, False, object())

        def _resolve_cred(self, context: object) -> object:
            return context

        def resolve_delete(self, _path: str, *, context: object) -> tuple[bool, object | None]:
            return (False, None)

        def _forget_mounted_backend_instance(self, _path: str) -> None:
            return None

    fs = _FS()

    assert fs.sys_unlink("/workspace/demo/delete-test.md") == {}

    fs._kernel.dispatch_post_hooks.assert_called_once()
    op_name, hook_ctx = fs._kernel.dispatch_post_hooks.call_args.args
    assert op_name == "delete"
    assert hook_ctx.path == "/workspace/demo/delete-test.md"

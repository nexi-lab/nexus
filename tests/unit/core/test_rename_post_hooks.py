"""Regression tests for rename post-hook dispatch in subprocess kernel mode."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from nexus.core.nexus_fs_metadata import MetadataMixin


def test_sys_rename_dispatches_python_post_hooks_when_kernel_result_does_not_request_them():
    """Remote kernels keep Python hooks locally, so result flags can be false."""

    kernel = MagicMock()
    kernel.hook_count.return_value = 1
    kernel.sys_rename.return_value = SimpleNamespace(
        post_hook_needed=False,
        old_content_id=None,
        old_size=None,
        old_modified_at_ms=None,
        old_version=None,
        is_directory=False,
    )

    fs = SimpleNamespace(
        _kernel=kernel,
        _gate_sys_namespace_mutation=lambda _paths, _context: None,
        _parse_context=lambda context: context,
        _prepare_rust_ctx=lambda context: ("default", "agent", False, "rust-context"),
    )

    MetadataMixin.sys_rename(fs, "/old.txt", "/new.txt")

    kernel.dispatch_post_hooks.assert_called_once()
    op, ctx = kernel.dispatch_post_hooks.call_args.args
    assert op == "rename"
    assert ctx.old_path == "/old.txt"
    assert ctx.new_path == "/new.txt"
    assert ctx.zone_id == "default"

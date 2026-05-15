"""Unit tests for nexus.fs._helpers module-level helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.fs._helpers import LOCAL_CONTEXT, close, list_mounts, unmount


def test_local_context_is_admin_local():
    assert LOCAL_CONTEXT.user_id == "local"
    assert LOCAL_CONTEXT.is_admin is True
    assert LOCAL_CONTEXT.zone_id == ROOT_ZONE_ID
    assert LOCAL_CONTEXT.groups == []


def test_list_mounts_returns_sorted_paths():
    py_kernel = MagicMock()
    py_kernel.get_mount_points.return_value = ["/root/zzz/b", "/root/aaa/a"]

    kernel = MagicMock()
    kernel._kernel = py_kernel

    with patch(
        "nexus.core.path_utils.extract_zone_id",
        side_effect=lambda c: (None, c.split("/", 2)[-1]),
    ):
        result = list_mounts(kernel)

    assert result == sorted(result)


def test_list_mounts_empty_when_no_inner_kernel():
    kernel = MagicMock()
    kernel._kernel = None
    assert list_mounts(kernel) == []


def test_unmount_rejects_non_mount_path():
    kernel = MagicMock()
    kernel.metadata.get.return_value = None

    with pytest.raises(ValueError, match="not a mount point"):
        unmount(kernel, "/not/a/mount")


def test_unmount_rejects_when_meta_not_mount():
    kernel = MagicMock()
    meta = MagicMock()
    meta.is_mount = False
    kernel.metadata.get.return_value = meta

    with pytest.raises(ValueError, match="not a mount point"):
        unmount(kernel, "/some/path")


def test_unmount_calls_sys_unlink_then_scrubs_mounts_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

    # Seed mounts.json with one entry that should be scrubbed
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps([{"uri": "local:///tmp/foo", "at": "/local/foo"}]))

    kernel = MagicMock()
    meta = MagicMock()
    meta.is_mount = True
    meta.zone_id = ROOT_ZONE_ID
    kernel.metadata.get.return_value = meta

    with (
        patch(
            "nexus.fs._uri.derive_mount_point",
            return_value="/local/foo",
        ),
        patch(
            "nexus.fs._uri.parse_uri",
            return_value=MagicMock(),
        ),
    ):
        unmount(kernel, "/local/foo")

    # The full unmount lifecycle is now a single sys_unlink call — the
    # kernel delegates to dlc::unmount internally when the entry is DT_MOUNT.
    kernel.sys_unlink.assert_called_once()
    args, kwargs = kernel.sys_unlink.call_args
    assert args[0] == "/local/foo"
    assert kwargs.get("context") is not None

    # mounts.json was rewritten without the entry
    remaining = json.loads(mounts_file.read_text())
    assert remaining == []


def test_close_calls_kernel_close_and_metastore_close():
    kernel = MagicMock()
    close(kernel)
    kernel.close.assert_called_once()
    kernel.metadata.close.assert_called_once()


def test_close_swallows_metastore_close_errors():
    kernel = MagicMock()
    kernel.metadata.close.side_effect = RuntimeError("oops")
    close(kernel)  # must not raise


# Phase 6/7 (sys_grep / sys_glob landed in PR #3921): the Python
# fallback path for grep/glob is gone — `nexus.fs._helpers.grep` and
# `glob` are now 1-line wrappers around `kernel.sys_grep` and
# `kernel.sys_glob`.  Tests that exercised the pre-Phase-6 Python
# `re.search` / `fnmatch` fallback are obsolete; the kernel-syscall
# path is exercised through `tests/unit/kernel/test_syscalls.py`
# (and the Rust-side `cargo test -p kernel sys_grep / sys_glob`).

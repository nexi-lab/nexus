"""sys_read DT_STREAM dict-return contract.

Covers the Python wrapper at ``nexus_fs_content.py`` where DT_STREAM reads
return ``{"data": bytes, "next_offset": int}`` so consumers can advance
their cursor without manually decoding the 4-byte LE frame header.
DT_REG / DT_PIPE return shapes are unchanged (bytes only).
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.nexus_fs import NexusFS


class _StubFS:
    """Minimal NexusFS double exercising only the sys_read DT_STREAM path."""

    def __init__(self, kernel: MagicMock) -> None:
        self._kernel = kernel
        self._zone_id = ROOT_ZONE_ID
        self._enforce_permissions = False
        self._init_cred = OperationContext(user_id="test", groups=[], zone_id=ROOT_ZONE_ID)
        self.resolve_read = MagicMock(return_value=(False, None))

    def _validate_path(self, path: str) -> str:
        return path

    def _parse_context(self, context):
        return context

    def _build_rust_ctx(self, context, is_admin):
        return SimpleNamespace(zone_id=self._zone_id, is_admin=is_admin)

    def _get_context_identity(self, context):
        # Issue #4081: sys_read hoists this call out of the post_hook block
        # so the OP emit can reuse the resolved agent_id. The stub returns
        # init_cred values; tests don't depend on a real subject identity.
        ctx = context or self._init_cred
        return (
            getattr(ctx, "zone_id", self._zone_id),
            getattr(ctx, "agent_id", None),
            getattr(ctx, "is_admin", False),
        )

    @contextlib.contextmanager
    def _vfs_locked(self, path, mode):
        yield


# Graft the real NexusFS.sys_read so we exercise the production dispatch.
_StubFS.sys_read = NexusFS.sys_read


class TestSysReadDtStream:
    def test_returns_dict_with_next_offset_when_data_available(self):
        kernel = MagicMock()
        # entry_type=4 selects the DT_STREAM branch.
        kernel.sys_read.return_value = SimpleNamespace(
            entry_type=4,
            data=b"hello",
            content_id=None,
            post_hook_needed=False,
            stream_next_offset=9,
        )

        fs = _StubFS(kernel)
        result = fs.sys_read("/s/test", offset=0)

        assert result == {"data": b"hello", "next_offset": 9}
        # next_offset must equal 4 (frame header) + len(payload) past the
        # request offset, so a follow-up read at next_offset advances cleanly.
        assert result["next_offset"] == 4 + len(result["data"])

    def test_falls_back_to_blocking_read_when_stream_empty(self):
        """Rust kernel handles blocking read end-to-end; Python just unpacks result."""
        kernel = MagicMock()
        kernel.sys_read.return_value = SimpleNamespace(
            entry_type=4,
            data=b"world",
            content_id=None,
            post_hook_needed=False,
            stream_next_offset=16,
        )

        fs = _StubFS(kernel)
        result = fs.sys_read("/s/test", offset=7)

        assert result == {"data": b"world", "next_offset": 16}

    def test_dt_reg_path_still_returns_bytes(self):
        # Regression: the dict shape is gated on entry_type==4 (DT_STREAM)
        # only — DT_REG must keep returning raw bytes so existing callers
        # are not silently broken.
        kernel = MagicMock()
        kernel.sys_read.return_value = SimpleNamespace(
            entry_type=1,
            data=b"file-bytes",
            content_id=None,
            post_hook_needed=False,
            stream_next_offset=None,
        )

        fs = _StubFS(kernel)
        result = fs.sys_read("/regular/file.txt")

        assert result == b"file-bytes"

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
            entry_type=4, data=None, content_id=None, post_hook_needed=False
        )
        # Hot path: stream_read_at returns (data, next_offset); next_offset
        # is `4-byte frame header + payload length` past the request offset.
        kernel.stream_read_at.return_value = (b"hello", 9)

        fs = _StubFS(kernel)
        result = fs.sys_read("/s/test", offset=0)

        assert result == {"data": b"hello", "next_offset": 9}
        # next_offset must equal 4 (frame header) + len(payload) past the
        # request offset, so a follow-up read at next_offset advances cleanly.
        assert result["next_offset"] == 4 + len(result["data"])
        kernel.stream_read_at.assert_called_once_with("/s/test", 0)
        kernel.stream_read_at_blocking.assert_not_called()

    def test_falls_back_to_blocking_read_when_stream_empty(self):
        kernel = MagicMock()
        kernel.sys_read.return_value = SimpleNamespace(
            entry_type=4, data=None, content_id=None, post_hook_needed=False
        )
        # Hot path returns None → wrapper blocks in Rust (GIL-free).
        kernel.stream_read_at.return_value = None
        kernel.stream_read_at_blocking.return_value = (b"world", 16)

        fs = _StubFS(kernel)
        result = fs.sys_read("/s/test", offset=7)

        assert result == {"data": b"world", "next_offset": 16}
        kernel.stream_read_at.assert_called_once_with("/s/test", 7)
        kernel.stream_read_at_blocking.assert_called_once_with("/s/test", 7, 30000)

    def test_dt_reg_path_still_returns_bytes(self):
        # Regression: the dict shape is gated on entry_type==4 (DT_STREAM)
        # only — DT_REG must keep returning raw bytes so existing callers
        # are not silently broken.
        kernel = MagicMock()
        kernel.sys_read.return_value = SimpleNamespace(
            entry_type=1, data=b"file-bytes", content_id=None, post_hook_needed=False
        )

        fs = _StubFS(kernel)
        result = fs.sys_read("/regular/file.txt")

        assert result == b"file-bytes"

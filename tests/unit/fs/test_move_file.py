"""Unit tests for nexus.sync.move_file fallback safety (Issue #341 hardening).

A failed sys_rename must NOT be masked by a copy+delete that clobbers the
destination or loses the source. These paths have non-existent parents so
move_file treats them as Nexus (not local) paths and takes the rename branch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from nexus.sync import move_file

_SRC = "/__nexus_vfs_test__/a/old.txt"
_DST = "/__nexus_vfs_test__/a/new.txt"


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_move_terminal_rename_error_does_not_clobber() -> None:
    """A FileExistsError from sys_rename is terminal: abort (False) without ever
    copy+deleting — never clobber the destination, never unlink the source."""
    nx = MagicMock()
    nx.sys_rename.side_effect = FileExistsError("dest exists")

    result = _run(move_file(nx, _SRC, _DST, force=False))

    assert result is False
    nx.write.assert_not_called()
    nx.sys_unlink.assert_not_called()


def test_move_permission_error_is_terminal() -> None:
    """A permission failure must not be downgraded into a copy+delete."""
    from nexus.contracts.exceptions import NexusPermissionError

    nx = MagicMock()
    nx.sys_rename.side_effect = NexusPermissionError("denied")

    result = _run(move_file(nx, _SRC, _DST, force=False))

    assert result is False
    nx.write.assert_not_called()
    nx.sys_unlink.assert_not_called()


def test_move_force_false_disables_copy_delete_fallback() -> None:
    """With force=False, a genuinely-unavailable rename does NOT fall back to the
    non-atomic copy+delete (which races a concurrent create); it aborts without
    writing — no check-then-write TOCTOU window."""
    nx = MagicMock()
    nx.sys_rename.side_effect = RuntimeError("gRPC transport unavailable")

    result = _run(move_file(nx, _SRC, _DST, force=False))

    assert result is False
    nx.write.assert_not_called()
    nx.sys_unlink.assert_not_called()


def test_move_fallback_copies_with_force_when_rename_unavailable() -> None:
    """Genuine rename-unavailable WITH force=True falls back to copy+delete —
    overwrite is explicitly requested (the original Issue #341 behavior)."""
    nx = MagicMock()
    nx.sys_rename.side_effect = RuntimeError("gRPC transport unavailable")
    nx.sys_read.return_value = b"data"

    result = _run(move_file(nx, _SRC, _DST, force=True))

    assert result is True
    nx.write.assert_called_once_with(_DST, b"data")
    nx.sys_unlink.assert_called_once_with(_SRC)

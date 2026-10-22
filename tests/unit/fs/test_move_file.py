"""Unit tests for nexus.sync.move_file fallback safety (Issue #341 hardening).

A failed sys_rename must NOT be masked by a copy+delete that clobbers the
destination or loses the source. These paths have non-existent parents so
move_file treats them as Nexus (not local) paths and takes the rename branch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.sync import move_file

_SRC = "/__nexus_vfs_test__/a/old.txt"
_DST = "/__nexus_vfs_test__/a/new.txt"


def _run(coro: Coroutine[Any, Any, bool]) -> bool:
    return asyncio.run(coro)


@pytest.mark.parametrize("force", [False, True])
def test_move_terminal_rename_error_does_not_clobber(force: bool) -> None:
    """A FileExistsError from sys_rename is terminal: abort (False) without ever
    copy+deleting — never clobber the destination, never unlink the source."""
    nx = MagicMock()
    nx.sys_rename.side_effect = FileExistsError("dest exists")

    result = _run(move_file(nx, _SRC, _DST, force=force))

    assert result is False
    nx.write.assert_not_called()
    nx.sys_unlink.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_move_permission_error_is_terminal(force: bool) -> None:
    """A permission failure must not be downgraded into a copy+delete."""
    from nexus.contracts.exceptions import NexusPermissionError

    nx = MagicMock()
    nx.sys_rename.side_effect = NexusPermissionError("denied")

    result = _run(move_file(nx, _SRC, _DST, force=force))

    assert result is False
    nx.write.assert_not_called()
    nx.sys_unlink.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_move_unavailable_rename_preserves_files(force: bool) -> None:
    """An unavailable rename leaves both files untouched."""
    nx = MagicMock()
    nx.sys_rename.side_effect = RuntimeError("gRPC transport unavailable")

    result = _run(move_file(nx, _SRC, _DST, force=force))

    assert result is False
    nx.write.assert_not_called()
    nx.sys_unlink.assert_not_called()

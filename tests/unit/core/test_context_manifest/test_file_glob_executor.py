"""Tests for FileGlobExecutor (Issue #1427).

Covers:
1. Happy path: glob matching files
2. Max files cap
3. Empty results
4. Path traversal rejection
5. Absolute path rejection
6. Symlink escape exclusion
7. Large directory metadata (total_matched vs returned)
8. Template variables in pattern
9. Nonexistent workspace root
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from nexus.core.context_manifest.executors.file_glob import FileGlobExecutor
from nexus.core.context_manifest.models import FileGlobSource


def _make_source(pattern: str = "*.txt", max_files: int = 50, **kw: Any) -> FileGlobSource:
    return FileGlobSource(pattern=pattern, max_files=max_files, **kw)


# ---------------------------------------------------------------------------
# Test 1: Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_glob_happy_path(self, tmp_path: Path) -> None:
        """3 .txt files, pattern *.txt → all 3 returned."""
        for name in ("a.txt", "b.txt", "c.txt"):
            (tmp_path / name).write_text(f"content of {name}")

        executor = FileGlobExecutor(workspace_root=tmp_path)
        result = await executor.execute(_make_source("*.txt"), {})

        assert result.status == "ok"
        assert result.data["returned"] == 3
        assert result.data["total_matched"] == 3
        assert set(result.data["files"].keys()) == {"a.txt", "b.txt", "c.txt"}
        assert result.data["files"]["a.txt"] == "content of a.txt"


# ---------------------------------------------------------------------------
# Test 2: Max files cap
# ---------------------------------------------------------------------------


class TestMaxFilesCap:
    @pytest.mark.asyncio
    async def test_max_files_cap(self, tmp_path: Path) -> None:
        """20 files, max_files=5 → only 5 returned, total_matched=20."""
        for i in range(20):
            (tmp_path / f"file_{i:02d}.txt").write_text(f"content {i}")

        executor = FileGlobExecutor(workspace_root=tmp_path)
        result = await executor.execute(_make_source("*.txt", max_files=5), {})

        assert result.status == "ok"
        assert result.data["total_matched"] == 20
        assert result.data["returned"] == 5
        assert len(result.data["files"]) == 5


# ---------------------------------------------------------------------------
# Test 3: Empty results
# ---------------------------------------------------------------------------


class TestEmptyResults:
    @pytest.mark.asyncio
    async def test_empty_results(self, tmp_path: Path) -> None:
        """No matches → ok with empty files dict."""
        executor = FileGlobExecutor(workspace_root=tmp_path)
        result = await executor.execute(_make_source("*.xyz"), {})

        assert result.status == "ok"
        assert result.data["files"] == {}
        assert result.data["total_matched"] == 0
        assert result.data["returned"] == 0


# ---------------------------------------------------------------------------
# Test 4: Path traversal rejected
# ---------------------------------------------------------------------------


class TestPathTraversal:
    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        """../../etc/passwd → error result."""
        executor = FileGlobExecutor(workspace_root=tmp_path)
        result = await executor.execute(_make_source("../../etc/passwd"), {})

        assert result.status == "error"
        assert "traversal" in result.error_message.lower()


# ---------------------------------------------------------------------------
# Test 5: Absolute path rejected
# ---------------------------------------------------------------------------


class TestAbsolutePath:
    @pytest.mark.asyncio
    async def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        """/etc/passwd → error result."""
        executor = FileGlobExecutor(workspace_root=tmp_path)
        result = await executor.execute(_make_source("/etc/passwd"), {})

        assert result.status == "error"
        assert "absolute" in result.error_message.lower()


# ---------------------------------------------------------------------------
# Test 6: Symlink escape excluded
# ---------------------------------------------------------------------------


class TestSymlinkEscape:
    @pytest.mark.asyncio
    async def test_symlink_escape_excluded(self, tmp_path: Path) -> None:
        """Symlink pointing outside workspace → excluded from results."""
        # Create a file outside workspace
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")

        # Create workspace with a symlink escaping to outside
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "normal.txt").write_text("normal")

        try:
            os.symlink(outside / "secret.txt", workspace / "link.txt")
        except OSError:
            pytest.skip("Cannot create symlinks on this platform")

        executor = FileGlobExecutor(workspace_root=workspace)
        result = await executor.execute(_make_source("*.txt"), {})

        assert result.status == "ok"
        # Only normal.txt should be returned (link.txt resolves outside workspace)
        assert result.data["returned"] == 1
        assert "normal.txt" in result.data["files"]
        assert "link.txt" not in result.data["files"]


# ---------------------------------------------------------------------------
# Test 7: Large directory metadata
# ---------------------------------------------------------------------------


class TestLargeDirectoryMetadata:
    @pytest.mark.asyncio
    async def test_large_directory_metadata(self, tmp_path: Path) -> None:
        """total_matched vs returned are correct when capped."""
        for i in range(15):
            (tmp_path / f"f{i}.py").write_text(f"# {i}")

        executor = FileGlobExecutor(workspace_root=tmp_path)
        result = await executor.execute(_make_source("*.py", max_files=3), {})

        assert result.status == "ok"
        assert result.data["total_matched"] == 15
        assert result.data["returned"] == 3


# ---------------------------------------------------------------------------
# Test 8: Template variables in pattern
# ---------------------------------------------------------------------------


class TestTemplateVariables:
    @pytest.mark.asyncio
    async def test_template_variables_in_pattern(self, tmp_path: Path) -> None:
        """Pattern with {{workspace.root}} is resolved before globbing."""
        # Create a subdir matching the variable value
        subdir = tmp_path / "src"
        subdir.mkdir()
        (subdir / "main.py").write_text("print('hello')")

        executor = FileGlobExecutor(workspace_root=tmp_path)
        variables = {"workspace.root": "src"}
        result = await executor.execute(_make_source("{{workspace.root}}/*.py"), variables)

        assert result.status == "ok"
        assert result.data["returned"] == 1


# ---------------------------------------------------------------------------
# Test 9: Nonexistent workspace root
# ---------------------------------------------------------------------------


class TestNonexistentWorkspaceRoot:
    @pytest.mark.asyncio
    async def test_nonexistent_workspace_root(self, tmp_path: Path) -> None:
        """Missing workspace dir → error result."""
        executor = FileGlobExecutor(workspace_root=tmp_path / "nonexistent")
        result = await executor.execute(_make_source("*.txt"), {})

        assert result.status == "error"
        assert "does not exist" in result.error_message.lower()

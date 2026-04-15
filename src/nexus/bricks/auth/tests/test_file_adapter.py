"""Tests for FileAdapter base class via synthetic adapter."""

from __future__ import annotations

import sys
from pathlib import Path

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import SyncedProfile, SyncResult
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter


class TestBaseImports:
    def test_base_types_importable(self) -> None:
        from nexus.bricks.auth.external_sync.base import (
            ExternalCliSyncAdapter,
            SyncedProfile,
        )

        assert SyncedProfile is not None
        assert SyncResult is not None
        assert ExternalCliSyncAdapter is not None


# ---------------------------------------------------------------------------
# Synthetic adapter helper
# ---------------------------------------------------------------------------


def _make_synthetic(
    tmp_path: Path | None,
    content: str = "",
    *,
    fail_parse: bool = False,
    setup_file: bool = True,
) -> FileAdapter:
    """Build a concrete FileAdapter subclass for testing.

    Parameters
    ----------
    tmp_path:
        pytest tmp_path fixture value. If None, a nonexistent path is used.
    content:
        Text to write into the synthetic config file.
    fail_parse:
        If True, parse_file raises ValueError.
    setup_file:
        If True (default) and tmp_path is not None, write the content to disk.
    """
    if tmp_path is not None:
        conf = tmp_path / "synthetic.conf"
        if setup_file and content:
            conf.write_text(content, encoding="utf-8")
        elif setup_file:
            # create empty file
            conf.write_text("", encoding="utf-8")
        file_path = conf
    else:
        file_path = Path("/nonexistent/does-not-exist/synthetic.conf")

    class SyntheticAdapter(FileAdapter):
        adapter_name = "synthetic-file"

        def paths(self) -> list[Path]:
            return [file_path]

        def parse_file(self, _path: Path, text: str) -> list[SyncedProfile]:
            if fail_parse:
                raise ValueError("deliberately malformed")
            lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
            return [
                SyncedProfile(
                    provider="synthetic",
                    account_identifier=line,
                    backend_key=f"synthetic/{line}",
                    source="synthetic-file",
                )
                for line in lines
            ]

        async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
            return ResolvedCredential(kind="api_key", api_key="fake-key")

    return SyntheticAdapter()


# ---------------------------------------------------------------------------
# Detect tests
# ---------------------------------------------------------------------------


class TestFileAdapterDetect:
    async def test_detect_true_when_file_exists(self, tmp_path: Path) -> None:
        adapter = _make_synthetic(tmp_path, content="profile1\n")
        assert await adapter.detect() is True

    async def test_detect_false_when_no_files(self) -> None:
        adapter = _make_synthetic(None)
        assert await adapter.detect() is False


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


class TestFileAdapterSync:
    async def test_sync_parses_file_content(self, tmp_path: Path) -> None:
        adapter = _make_synthetic(tmp_path, content="alice\nbob\n")
        result = await adapter.sync()

        assert result.adapter_name == "synthetic-file"
        assert result.error is None
        assert len(result.profiles) == 2
        assert result.profiles[0].account_identifier == "alice"
        assert result.profiles[1].account_identifier == "bob"

    async def test_sync_missing_file_returns_degraded(self) -> None:
        adapter = _make_synthetic(None, setup_file=False)
        result = await adapter.sync()

        assert result.error is not None
        assert "no readable" in result.error.lower() or "No readable" in result.error

    async def test_sync_empty_file_returns_empty_profiles(self, tmp_path: Path) -> None:
        adapter = _make_synthetic(tmp_path, content="")
        result = await adapter.sync()

        assert result.profiles == []
        assert result.error is None

    async def test_sync_malformed_content_returns_degraded(self, tmp_path: Path) -> None:
        adapter = _make_synthetic(tmp_path, content="bad-data\n", fail_parse=True)
        result = await adapter.sync()

        assert result.error is not None
        assert "parse error" in result.error.lower()

    async def test_sync_unreadable_perms_returns_degraded(self, tmp_path: Path) -> None:
        if sys.platform == "win32":
            return  # chmod 000 doesn't work on Windows

        adapter = _make_synthetic(tmp_path, content="profile1\n")
        conf = tmp_path / "synthetic.conf"
        original_mode = conf.stat().st_mode
        try:
            conf.chmod(0o000)
            result = await adapter.sync()
            assert result.error is not None
            assert "permission denied" in result.error.lower() or "No readable" in result.error
        finally:
            conf.chmod(original_mode)

    async def test_sync_symlink_loop_returns_degraded(self, tmp_path: Path) -> None:
        # Create a symlink loop: a.conf -> b.conf -> a.conf
        a = tmp_path / "synthetic.conf"
        b = tmp_path / "b.conf"

        # Remove any existing file first (created by _make_synthetic)
        if a.exists():
            a.unlink()

        b.symlink_to(a)
        a.symlink_to(b)

        # Build adapter that points at the symlink loop
        class LoopAdapter(FileAdapter):
            adapter_name = "synthetic-file"

            def paths(self) -> list[Path]:
                return [a]

            def parse_file(self, _path: Path, _text: str) -> list[SyncedProfile]:
                return []

            async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
                return ResolvedCredential(kind="api_key", api_key="fake-key")

        adapter = LoopAdapter()
        result = await adapter.sync()

        # Symlink loop causes OSError on read_text — should be caught gracefully
        assert result.error is not None

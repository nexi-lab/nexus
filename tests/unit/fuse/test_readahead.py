"""Tests for ReadaheadManager per-session IOProfile override (Issue #1413).

Tests that ReadaheadConfig.from_io_profile() produces correct values
and that on_open() with io_profile creates sessions with profile-derived params.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.contracts.io_profile import IOProfile
from nexus.fuse.readahead import ReadaheadConfig, ReadaheadManager, ReadSession


class TestReadaheadConfigFromIOProfile:
    """Test ReadaheadConfig.from_io_profile() class method."""

    def test_fast_read_profile(self) -> None:
        cfg = ReadaheadConfig.from_io_profile(IOProfile.FAST_READ)
        assert cfg.enabled is True
        assert cfg.initial_window == 512 * 1024
        assert cfg.max_window == 64 * 1024 * 1024
        assert cfg.prefetch_workers == 8
        assert cfg.prefetch_on_open is True

    def test_fast_write_profile_disabled(self) -> None:
        cfg = ReadaheadConfig.from_io_profile(IOProfile.FAST_WRITE)
        assert cfg.enabled is False
        assert cfg.prefetch_workers == 0

    def test_edit_profile(self) -> None:
        cfg = ReadaheadConfig.from_io_profile(IOProfile.EDIT)
        assert cfg.enabled is True
        assert cfg.initial_window == 256 * 1024
        assert cfg.max_window == 1 * 1024 * 1024
        assert cfg.prefetch_workers == 2
        assert cfg.prefetch_on_open is False

    def test_balanced_profile(self) -> None:
        cfg = ReadaheadConfig.from_io_profile(IOProfile.BALANCED)
        assert cfg.enabled is True
        assert cfg.initial_window == 512 * 1024
        assert cfg.max_window == 32 * 1024 * 1024
        assert cfg.prefetch_workers == 4

    def test_archive_profile_disabled(self) -> None:
        cfg = ReadaheadConfig.from_io_profile(IOProfile.ARCHIVE)
        assert cfg.enabled is False

    @pytest.mark.parametrize("profile", list(IOProfile))
    def test_all_profiles_return_valid_config(self, profile: IOProfile) -> None:
        cfg = ReadaheadConfig.from_io_profile(profile)
        assert isinstance(cfg, ReadaheadConfig)


class TestReadSessionOverrides:
    """Test ReadSession with custom initial_window and max_window."""

    def test_custom_initial_window(self) -> None:
        session = ReadSession(
            path="/test",
            fh=1,
            readahead_window=256 * 1024,
            max_window=1 * 1024 * 1024,
        )
        assert session.readahead_window == 256 * 1024
        assert session.max_window == 1 * 1024 * 1024

    def test_default_window(self) -> None:
        session = ReadSession(path="/test", fh=1)
        # Default from module constants
        assert session.readahead_window == 512 * 1024
        assert session.max_window == 64 * 1024 * 1024


class TestReadaheadManagerOnOpen:
    """Test on_open() with io_profile creates correctly parameterized sessions."""

    def _make_manager(self, config: ReadaheadConfig | None = None) -> ReadaheadManager:
        if config is None:
            config = ReadaheadConfig(enabled=True, prefetch_on_open=False)
        read_func = MagicMock(return_value=b"data")
        return ReadaheadManager(config=config, read_func=read_func)

    def test_on_open_with_io_profile_overrides_session(self) -> None:
        manager = self._make_manager()
        manager.on_open(fh=1, path="/test", file_size=1000, io_profile=IOProfile.EDIT)

        # Check that the session was created with EDIT profile params
        session = manager._sessions.get(1)
        assert session is not None
        assert session.readahead_window == 256 * 1024
        assert session.max_window == 1 * 1024 * 1024

    def test_on_open_with_fast_read_profile(self) -> None:
        manager = self._make_manager()
        manager.on_open(
            fh=2, path="/weights", file_size=100_000_000, io_profile=IOProfile.FAST_READ
        )

        session = manager._sessions.get(2)
        assert session is not None
        assert session.max_window == 64 * 1024 * 1024

    def test_on_open_without_io_profile_uses_defaults(self) -> None:
        config = ReadaheadConfig(enabled=True, prefetch_on_open=True)
        manager = self._make_manager(config)
        manager.on_open(fh=3, path="/default", file_size=1000)

        session = manager._sessions.get(3)
        assert session is not None
        # Uses manager's default config
        assert session.max_window == manager._config.max_window

    def test_on_open_disabled_profile_skips_session(self) -> None:
        config = ReadaheadConfig(enabled=True, prefetch_on_open=True)
        manager = self._make_manager(config)
        manager.on_open(fh=4, path="/logs", file_size=1000, io_profile=IOProfile.FAST_WRITE)

        # FAST_WRITE disables readahead, so no session should be created
        assert 4 not in manager._sessions

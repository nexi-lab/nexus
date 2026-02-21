"""Tests for IOProfile enum and IOProfileConfig data model (Issue #1413).

TDD: Tests written first, implementation follows.
"""

import pytest


class TestIOProfileEnum:
    """Test IOProfile enum values and behavior."""

    def test_has_six_values(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        assert len(IOProfile) == 6

    def test_string_values(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        assert IOProfile.FAST_READ == "fast_read"
        assert IOProfile.FAST_WRITE == "fast_write"
        assert IOProfile.EDIT == "edit"
        assert IOProfile.APPEND_ONLY == "append_only"
        assert IOProfile.BALANCED == "balanced"
        assert IOProfile.ARCHIVE == "archive"

    def test_str_conversion(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        assert str(IOProfile.FAST_READ) == "fast_read"
        assert str(IOProfile.BALANCED) == "balanced"

    def test_from_string(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        assert IOProfile("fast_read") is IOProfile.FAST_READ
        assert IOProfile("balanced") is IOProfile.BALANCED

    def test_invalid_value_raises(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        with pytest.raises(ValueError):
            IOProfile("nonexistent")

    def test_config_method_returns_profile_config(self) -> None:
        from nexus.contracts.io_profile import IOProfile, IOProfileConfig

        cfg = IOProfile.BALANCED.config()
        assert isinstance(cfg, IOProfileConfig)


class TestIOProfileConfig:
    """Test IOProfileConfig frozen dataclass."""

    def test_frozen_dataclass(self) -> None:
        from nexus.contracts.io_profile import IOProfileConfig

        cfg = IOProfileConfig(
            readahead_enabled=True,
            readahead_initial_window=512 * 1024,
            readahead_max_window=32 * 1024 * 1024,
            readahead_workers=4,
            readahead_prefetch_on_open=True,
            cache_priority=2,
            write_buffer_flush_interval_ms=100,
            write_buffer_max_size=100,
            write_buffer_sync_mode=False,
        )
        with pytest.raises(AttributeError):
            cfg.readahead_enabled = False  # type: ignore[misc]

    def test_all_fields_accessible(self) -> None:
        from nexus.contracts.io_profile import IOProfileConfig

        cfg = IOProfileConfig(
            readahead_enabled=True,
            readahead_initial_window=512 * 1024,
            readahead_max_window=32 * 1024 * 1024,
            readahead_workers=4,
            readahead_prefetch_on_open=True,
            cache_priority=2,
            write_buffer_flush_interval_ms=100,
            write_buffer_max_size=100,
            write_buffer_sync_mode=False,
        )
        assert cfg.readahead_enabled is True
        assert cfg.readahead_initial_window == 512 * 1024
        assert cfg.readahead_max_window == 32 * 1024 * 1024
        assert cfg.readahead_workers == 4
        assert cfg.readahead_prefetch_on_open is True
        assert cfg.cache_priority == 2
        assert cfg.write_buffer_flush_interval_ms == 100
        assert cfg.write_buffer_max_size == 100
        assert cfg.write_buffer_sync_mode is False


class TestProfileConfigMapping:
    """Test _PROFILE_CONFIGS mapping covers all profiles correctly."""

    @pytest.mark.parametrize(
        "profile_name",
        ["fast_read", "fast_write", "edit", "append_only", "balanced", "archive"],
    )
    def test_each_profile_has_config(self, profile_name: str) -> None:
        from nexus.contracts.io_profile import IOProfile

        profile = IOProfile(profile_name)
        cfg = profile.config()
        assert cfg is not None

    @pytest.mark.parametrize(
        ("profile_name", "expected_readahead"),
        [
            ("fast_read", True),
            ("fast_write", False),
            ("edit", True),
            ("append_only", False),
            ("balanced", True),
            ("archive", False),
        ],
    )
    def test_readahead_enabled_per_profile(
        self, profile_name: str, expected_readahead: bool
    ) -> None:
        from nexus.contracts.io_profile import IOProfile

        cfg = IOProfile(profile_name).config()
        assert cfg.readahead_enabled is expected_readahead

    @pytest.mark.parametrize(
        ("profile_name", "expected_priority"),
        [
            ("fast_read", 3),
            ("fast_write", 1),
            ("edit", 2),
            ("append_only", 0),
            ("balanced", 2),
            ("archive", 0),
        ],
    )
    def test_cache_priority_per_profile(self, profile_name: str, expected_priority: int) -> None:
        from nexus.contracts.io_profile import IOProfile

        cfg = IOProfile(profile_name).config()
        assert cfg.cache_priority == expected_priority

    def test_fast_read_has_highest_readahead_window(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        fast_read = IOProfile.FAST_READ.config()
        balanced = IOProfile.BALANCED.config()
        assert fast_read.readahead_max_window >= balanced.readahead_max_window

    def test_fast_write_has_largest_write_buffer(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        fast_write = IOProfile.FAST_WRITE.config()
        balanced = IOProfile.BALANCED.config()
        assert fast_write.write_buffer_max_size >= balanced.write_buffer_max_size

    def test_archive_all_zeroes(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        cfg = IOProfile.ARCHIVE.config()
        assert cfg.readahead_enabled is False
        assert cfg.readahead_initial_window == 0
        assert cfg.readahead_max_window == 0
        assert cfg.readahead_workers == 0
        assert cfg.cache_priority == 0
        assert cfg.write_buffer_flush_interval_ms == 0
        assert cfg.write_buffer_max_size == 0

    def test_edit_sync_mode_enabled(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        cfg = IOProfile.EDIT.config()
        assert cfg.write_buffer_sync_mode is True

    def test_fast_read_prefetch_on_open(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        cfg = IOProfile.FAST_READ.config()
        assert cfg.readahead_prefetch_on_open is True


class TestReadaheadConfigDerivation:
    """Test IOProfile → ReadaheadConfig conversion."""

    def test_from_io_profile_fast_read(self) -> None:
        from nexus.contracts.io_profile import IOProfile
        from nexus.fuse.readahead import ReadaheadConfig

        cfg = ReadaheadConfig.from_io_profile(IOProfile.FAST_READ)
        assert cfg.enabled is True
        assert cfg.initial_window == 512 * 1024
        assert cfg.max_window == 64 * 1024 * 1024
        assert cfg.prefetch_workers == 8
        assert cfg.prefetch_on_open is True

    def test_from_io_profile_fast_write_disabled(self) -> None:
        from nexus.contracts.io_profile import IOProfile
        from nexus.fuse.readahead import ReadaheadConfig

        cfg = ReadaheadConfig.from_io_profile(IOProfile.FAST_WRITE)
        assert cfg.enabled is False

    def test_from_io_profile_balanced(self) -> None:
        from nexus.contracts.io_profile import IOProfile
        from nexus.fuse.readahead import ReadaheadConfig

        cfg = ReadaheadConfig.from_io_profile(IOProfile.BALANCED)
        assert cfg.enabled is True
        assert cfg.initial_window == 512 * 1024
        assert cfg.max_window == 32 * 1024 * 1024
        assert cfg.prefetch_workers == 4

    @pytest.mark.parametrize(
        "profile_name",
        ["fast_read", "fast_write", "edit", "append_only", "balanced", "archive"],
    )
    def test_all_profiles_produce_valid_config(self, profile_name: str) -> None:
        from nexus.contracts.io_profile import IOProfile
        from nexus.fuse.readahead import ReadaheadConfig

        cfg = ReadaheadConfig.from_io_profile(IOProfile(profile_name))
        assert isinstance(cfg, ReadaheadConfig)


class TestCachePriorityMapping:
    """Test IOProfile → cache priority values."""

    def test_priority_range(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        for profile in IOProfile:
            priority = profile.config().cache_priority
            assert 0 <= priority <= 3, f"{profile}: priority {priority} out of range"

    def test_fast_read_highest_priority(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        assert IOProfile.FAST_READ.config().cache_priority == 3

    def test_archive_lowest_priority(self) -> None:
        from nexus.contracts.io_profile import IOProfile

        assert IOProfile.ARCHIVE.config().cache_priority == 0

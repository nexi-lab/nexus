"""Tests for SANDBOX profile config defaults (Issue #3778).

Uses `model_fields_set` (pydantic v2) to distinguish explicitly-provided
fields from defaulted ones.  Only fields absent from `model_fields_set` get
overridden by _apply_sandbox_defaults; user values always win, even when
they happen to equal a system default.

Field-type notes:
- NexusConfig.backend is str (not Optional[str]); default "path_local".
- NexusConfig.cache_size_mb is int (not Optional[int]); default 100.
- NexusConfig.enable_vector_search is bool (not Optional[bool]); default True.
- data_dir, db_path, metastore_path, record_store_path are Optional[str] = None.
"""

from nexus.config import NexusConfig, _apply_sandbox_defaults


class TestApplySandboxDefaults:
    def test_non_sandbox_profile_is_untouched(self) -> None:
        cfg = NexusConfig(profile="full")
        result = _apply_sandbox_defaults(cfg)
        assert result.data_dir == cfg.data_dir
        assert result.backend == cfg.backend

    def test_sandbox_sets_local_backend_when_at_system_default(self) -> None:
        # backend NOT provided — not in model_fields_set — should get sandbox default
        cfg = NexusConfig(profile="sandbox")
        result = _apply_sandbox_defaults(cfg)
        assert result.backend == "local"

    def test_sandbox_sets_data_dir(self) -> None:
        # data_dir NOT provided — not in model_fields_set — should get sandbox default
        cfg = NexusConfig(profile="sandbox")
        result = _apply_sandbox_defaults(cfg)
        assert result.data_dir is not None
        assert "sandbox" in result.data_dir

    def test_sandbox_sets_sqlite_paths(self) -> None:
        cfg = NexusConfig(profile="sandbox", data_dir="/tmp/test-sandbox")
        result = _apply_sandbox_defaults(cfg)
        assert result.db_path == "/tmp/test-sandbox/nexus.db"
        assert result.metastore_path == "/tmp/test-sandbox/nexus.db"
        assert result.record_store_path == "/tmp/test-sandbox/nexus.db"

    def test_sandbox_cache_size_default(self) -> None:
        # cache_size_mb NOT provided — not in model_fields_set — should get sandbox default
        cfg = NexusConfig(profile="sandbox")
        result = _apply_sandbox_defaults(cfg)
        assert result.cache_size_mb == 64

    def test_sandbox_vector_search_default_off(self) -> None:
        # enable_vector_search NOT provided — not in model_fields_set — should get sandbox default
        cfg = NexusConfig(profile="sandbox")
        result = _apply_sandbox_defaults(cfg)
        assert result.enable_vector_search is False

    def test_explicit_user_values_win(self) -> None:
        # Use "path_gcs" as an explicit non-default backend override
        cfg = NexusConfig(
            profile="sandbox",
            backend="path_gcs",
            data_dir="/custom/path",
            cache_size_mb=512,
            enable_vector_search=False,
        )
        result = _apply_sandbox_defaults(cfg)
        assert result.backend == "path_gcs"
        assert result.data_dir == "/custom/path"
        assert result.cache_size_mb == 512
        # enable_vector_search=False was already set — should not be overwritten
        assert result.enable_vector_search is False

    def test_user_set_data_dir_drives_db_paths(self) -> None:
        """If user sets data_dir but not db_path, db_path should derive from data_dir."""
        # db_path, metastore_path, record_store_path NOT provided — not in model_fields_set
        cfg = NexusConfig(
            profile="sandbox",
            data_dir="/my/custom",
        )
        result = _apply_sandbox_defaults(cfg)
        assert result.db_path == "/my/custom/nexus.db"
        assert result.metastore_path == "/my/custom/nexus.db"
        assert result.record_store_path == "/my/custom/nexus.db"

    def test_returns_same_object_when_no_updates(self) -> None:
        """When all sandbox defaults are already applied, returns same cfg."""
        cfg = NexusConfig(
            profile="sandbox",
            backend="local",
            data_dir=str(__import__("pathlib").Path.home() / ".nexus" / "sandbox"),
            db_path=str(__import__("pathlib").Path.home() / ".nexus" / "sandbox" / "nexus.db"),
            metastore_path=str(
                __import__("pathlib").Path.home() / ".nexus" / "sandbox" / "nexus.db"
            ),
            record_store_path=str(
                __import__("pathlib").Path.home() / ".nexus" / "sandbox" / "nexus.db"
            ),
            cache_size_mb=64,
            enable_vector_search=False,
        )
        result = _apply_sandbox_defaults(cfg)
        # All fields are in model_fields_set — no updates computed — same object returned
        assert result is cfg

    def test_explicit_user_value_equal_to_default_preserved(self) -> None:
        """A user who explicitly sets cache_size_mb=100 (a common default) must
        NOT have it silently overridden to 64 in sandbox mode.
        Regression: prior sentinel-based detection treated 100 as 'unset'."""
        cfg = NexusConfig(profile="sandbox", cache_size_mb=100)
        result = _apply_sandbox_defaults(cfg)
        assert result.cache_size_mb == 100


class TestLoadFromDictSandbox:
    def test_data_dir_override_rederives_sqlite_paths(self) -> None:
        """Regression: user-supplied data_dir via _load_from_dict must update
        all SQLite path fields — bug from Task 14's integration test.

        _load_from_environment() may stamp sandbox paths derived from the
        default ~/.nexus/sandbox data_dir.  After model_dump() + update() in
        _load_from_dict, those stale paths appear in the merged dict even when
        the user only specified data_dir=/tmp/custom.  _apply_sandbox_defaults
        then sees path fields already "present" and skips re-deriving them.
        The fix strips stale path fields from merged_dict before constructing
        the final NexusConfig, so _apply_sandbox_defaults can re-derive them
        from the user's custom data_dir.
        """
        from nexus.config import _load_from_dict

        cfg = _load_from_dict({"profile": "sandbox", "data_dir": "/tmp/custom"})
        assert cfg.data_dir == "/tmp/custom"
        assert cfg.db_path == "/tmp/custom/nexus.db"
        assert cfg.metastore_path == "/tmp/custom/nexus.db"
        assert cfg.record_store_path == "/tmp/custom/nexus.db"

    def test_explicit_path_fields_in_dict_are_not_overridden(self) -> None:
        """If the user explicitly provides db_path in config_dict, it must win
        over any data_dir-derived value (the strip only fires when path fields
        are absent from the user's dict)."""
        from nexus.config import _load_from_dict

        cfg = _load_from_dict(
            {
                "profile": "sandbox",
                "data_dir": "/tmp/custom",
                "db_path": "/tmp/custom/override.db",
                "metastore_path": "/tmp/custom/override.db",
                "record_store_path": "/tmp/custom/override.db",
            }
        )
        assert cfg.data_dir == "/tmp/custom"
        assert cfg.db_path == "/tmp/custom/override.db"
        assert cfg.metastore_path == "/tmp/custom/override.db"
        assert cfg.record_store_path == "/tmp/custom/override.db"

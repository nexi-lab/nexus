"""Tests for SANDBOX profile config defaults (Issue #3778).

Field-type notes vs the original task spec:
- NexusConfig.backend is str (not Optional[str]); default "path_local".
  _apply_sandbox_defaults treats "path_local" as "unset" for sandbox
  and upgrades it to "local".
- NexusConfig.cache_size_mb is int (not Optional[int]); default 100.
  _apply_sandbox_defaults treats 100 as "unset" and replaces with 64.
- NexusConfig.enable_vector_search is bool (not Optional[bool]); default True.
  _apply_sandbox_defaults treats True as "unset" and replaces with False.
- data_dir, db_path, metastore_path, record_store_path are Optional[str] = None,
  so None-based sentinel logic works unchanged for those fields.
"""

from nexus.config import NexusConfig, _apply_sandbox_defaults


class TestApplySandboxDefaults:
    def test_non_sandbox_profile_is_untouched(self) -> None:
        cfg = NexusConfig(profile="full", data_dir=None)
        result = _apply_sandbox_defaults(cfg)
        assert result.data_dir == cfg.data_dir
        assert result.backend == cfg.backend

    def test_sandbox_sets_local_backend_when_at_system_default(self) -> None:
        # backend="path_local" is the NexusConfig default — treated as "unset"
        cfg = NexusConfig(profile="sandbox", backend="path_local")
        result = _apply_sandbox_defaults(cfg)
        assert result.backend == "local"

    def test_sandbox_sets_data_dir(self) -> None:
        cfg = NexusConfig(profile="sandbox", data_dir=None)
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
        # cache_size_mb=100 is the NexusConfig default — treated as "unset"
        cfg = NexusConfig(profile="sandbox", cache_size_mb=100)
        result = _apply_sandbox_defaults(cfg)
        assert result.cache_size_mb == 64

    def test_sandbox_vector_search_default_off(self) -> None:
        # enable_vector_search=True is the NexusConfig default — treated as "unset"
        cfg = NexusConfig(profile="sandbox", enable_vector_search=True)
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
        cfg = NexusConfig(
            profile="sandbox",
            data_dir="/my/custom",
            db_path=None,
            metastore_path=None,
            record_store_path=None,
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
        # No updates needed — should return the same object
        assert result is cfg

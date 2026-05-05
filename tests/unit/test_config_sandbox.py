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

from nexus.config import NexusConfig, _apply_sandbox_defaults, load_config


class TestApplySandboxDefaults:
    def test_non_sandbox_profile_is_untouched(self) -> None:
        cfg = NexusConfig(profile="full")
        result = _apply_sandbox_defaults(cfg)
        assert result.data_dir == cfg.data_dir
        assert result.backend == cfg.backend

    def test_sandbox_sets_local_backend_when_at_system_default(self) -> None:
        # backend NOT provided — not in model_fields_set — should get sandbox default.
        # R1 review (#3778): sandbox uses path_local (direct FS) not local (CAS) —
        # CAS has hash-store overhead that defeats the "lightweight sandbox" intent.
        cfg = NexusConfig(profile="sandbox")
        result = _apply_sandbox_defaults(cfg)
        assert result.backend == "path_local"

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

    def test_sandbox_vector_search_default_on(self) -> None:
        # PR #4022 + Codex review R3: SANDBOX is now vec-ON by default.
        # The [sandbox] extra bundles sqlite-vec + fastembed so offline
        # embeddings work out of the box; the schema default (True) is
        # the right behavior for SANDBOX too. Users opt out via
        # enable_vector_search=False (config dict or env).
        cfg = NexusConfig(profile="sandbox")
        result = _apply_sandbox_defaults(cfg)
        assert result.enable_vector_search is True

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


class TestLoadConfigNexusConfigPassthroughNormalizes:
    """Codex review R5 #3 (medium): ``load_config(NexusConfig(...))`` was
    a passthrough that skipped ``_apply_sandbox_defaults``. dict/YAML
    inputs flow through it. The asymmetry meant a SANDBOX-typed
    NexusConfig instance left ``db_path`` unset, which broke local
    sqlite-vec wiring (no DB path resolved → vec backend skipped)."""

    def test_nexusconfig_sandbox_passes_through_defaulter(self) -> None:
        cfg_in = NexusConfig(profile="sandbox")
        cfg_out = load_config(cfg_in)
        # The defaulter must have run: backend, data_dir, db paths,
        # cache_size all populated.
        assert cfg_out.backend == "path_local"
        assert cfg_out.data_dir is not None and "sandbox" in cfg_out.data_dir
        assert cfg_out.db_path is not None and cfg_out.db_path.endswith("nexus.db")
        assert cfg_out.metastore_path == cfg_out.db_path
        assert cfg_out.record_store_path == cfg_out.db_path
        # Vec-on-by-default still applies.
        assert cfg_out.enable_vector_search is True

    def test_nexusconfig_explicit_user_values_still_win(self) -> None:
        """Defaulter respects model_fields_set on a NexusConfig too —
        an explicit ``enable_vector_search=False`` must survive
        passthrough+normalize."""
        cfg_in = NexusConfig(
            profile="sandbox",
            data_dir="/tmp/explicit",
            enable_vector_search=False,
        )
        cfg_out = load_config(cfg_in)
        assert cfg_out.data_dir == "/tmp/explicit"
        assert cfg_out.enable_vector_search is False

    def test_nexusconfig_non_sandbox_unchanged(self) -> None:
        """Non-sandbox profiles must be left alone by the defaulter
        even when routed through ``load_config(NexusConfig)``."""
        cfg_in = NexusConfig(profile="full")
        cfg_out = load_config(cfg_in)
        assert cfg_out.profile == "full"
        # data_dir must NOT be coerced to a sandbox path.
        if cfg_out.data_dir is not None:
            assert "sandbox" not in cfg_out.data_dir

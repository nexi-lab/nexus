"""Tests for OverlayResolver service.

Issue #1264: CAS dedup at VFS level — ComposeFS-style agent workspace overlays.
Pattern follows: tests/unit/core/test_workspace_manifest.py
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core._metadata_generated import FileMetadata
from nexus.core.workspace_manifest import ManifestEntry, WorkspaceManifest
from nexus.services.overlay_resolver import (
    WHITEOUT_HASH,
    OverlayConfig,
    OverlayResolver,
    OverlayStats,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_metadata() -> MagicMock:
    """Mock FileMetadataProtocol."""
    metadata = MagicMock()
    metadata.get.return_value = None
    metadata.list.return_value = []
    metadata.exists.return_value = False
    return metadata


@pytest.fixture
def mock_backend() -> MagicMock:
    """Mock Backend with CAS operations."""
    backend = MagicMock()
    return backend


@pytest.fixture
def base_manifest() -> WorkspaceManifest:
    """A sample base manifest with 3 files."""
    return WorkspaceManifest(
        entries={
            "src/main.py": ManifestEntry(
                content_hash="hash_main", size=1000, mime_type="text/x-python"
            ),
            "src/utils.py": ManifestEntry(
                content_hash="hash_utils", size=500, mime_type="text/x-python"
            ),
            "README.md": ManifestEntry(
                content_hash="hash_readme", size=200, mime_type="text/markdown"
            ),
        }
    )


@pytest.fixture
def overlay_config() -> OverlayConfig:
    """Standard overlay config for tests."""
    return OverlayConfig(
        enabled=True,
        base_manifest_hash="manifest_hash_abc",
        workspace_path="/workspace",
        agent_id="agent-001",
    )


@pytest.fixture
def resolver(
    mock_metadata: MagicMock, mock_backend: MagicMock, base_manifest: WorkspaceManifest
) -> OverlayResolver:
    """OverlayResolver with pre-cached base manifest."""
    resolver = OverlayResolver(metadata=mock_metadata, backend=mock_backend)
    # Pre-cache the manifest to avoid backend calls in most tests
    resolver._manifest_cache["manifest_hash_abc"] = base_manifest
    return resolver


def _make_file_meta(
    path: str,
    etag: str = "test_hash",
    size: int = 100,
    mime_type: str | None = "text/plain",
) -> FileMetadata:
    """Helper to create FileMetadata for tests."""
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=etag,
        size=size,
        etag=etag,
        mime_type=mime_type,
    )


def _make_whiteout(path: str, agent_id: str = "agent-001") -> FileMetadata:
    """Helper to create whiteout FileMetadata."""
    return FileMetadata(
        path=path,
        backend_name="overlay",
        physical_path=WHITEOUT_HASH,
        size=0,
        etag=WHITEOUT_HASH,
        mime_type=None,
        created_by=agent_id,
    )


# =============================================================================
# OverlayConfig Tests
# =============================================================================


class TestOverlayConfig:
    """Tests for OverlayConfig dataclass."""

    def test_defaults(self) -> None:
        config = OverlayConfig()
        assert config.enabled is False
        assert config.base_manifest_hash is None
        assert config.workspace_path == ""
        assert config.agent_id is None

    def test_enabled_config(self) -> None:
        config = OverlayConfig(
            enabled=True,
            base_manifest_hash="abc123",
            workspace_path="/ws",
            agent_id="agent-1",
        )
        assert config.enabled is True
        assert config.base_manifest_hash == "abc123"


# =============================================================================
# OverlayStats Tests
# =============================================================================


class TestOverlayStats:
    """Tests for OverlayStats dataclass."""

    def test_defaults(self) -> None:
        stats = OverlayStats()
        assert stats.total_files == 0
        assert stats.shared_ratio == 0.0

    def test_to_dict(self) -> None:
        stats = OverlayStats(
            total_files=10,
            base_files=7,
            upper_files=3,
            whiteout_count=1,
            shared_ratio=0.7,
            estimated_savings_bytes=5000,
        )
        d = stats.to_dict()
        assert d["total_files"] == 10
        assert d["shared_ratio"] == 0.7
        assert d["estimated_savings_bytes"] == 5000


# =============================================================================
# Resolution Tests
# =============================================================================


class TestResolveRead:
    """Tests for resolve_read() — the core overlay resolution."""

    def test_upper_layer_hit(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Upper layer entry takes precedence over base."""
        upper_meta = _make_file_meta("/workspace/src/main.py", etag="modified_hash", size=1500)
        mock_metadata.get.return_value = upper_meta

        result = resolver.resolve_read("/workspace/src/main.py", overlay_config)

        assert result is not None
        assert result.etag == "modified_hash"
        assert result.size == 1500

    def test_base_layer_hit(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Falls back to base layer when upper has no entry."""
        mock_metadata.get.return_value = None

        result = resolver.resolve_read("/workspace/src/main.py", overlay_config)

        assert result is not None
        assert result.etag == "hash_main"
        assert result.size == 1000
        assert result.mime_type == "text/x-python"

    def test_whiteout_hides_base(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Whiteout in upper layer hides base-layer file."""
        mock_metadata.get.return_value = _make_whiteout("/workspace/README.md")

        result = resolver.resolve_read("/workspace/README.md", overlay_config)

        assert result is not None
        assert resolver.is_whiteout(result)

    def test_double_miss(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Returns None when file exists in neither layer."""
        mock_metadata.get.return_value = None

        result = resolver.resolve_read("/workspace/nonexistent.txt", overlay_config)

        assert result is None

    def test_disabled_overlay_returns_none(
        self,
        resolver: OverlayResolver,
    ) -> None:
        """Disabled overlay config always returns None."""
        config = OverlayConfig(enabled=False)
        result = resolver.resolve_read("/workspace/src/main.py", config)
        assert result is None

    def test_no_base_hash_returns_none(
        self,
        resolver: OverlayResolver,
    ) -> None:
        """Missing base_manifest_hash returns None."""
        config = OverlayConfig(enabled=True, base_manifest_hash=None)
        result = resolver.resolve_read("/workspace/src/main.py", config)
        assert result is None

    def test_path_outside_workspace_returns_none(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Path not under workspace prefix returns None."""
        mock_metadata.get.return_value = None

        result = resolver.resolve_read("/other-workspace/file.txt", overlay_config)

        assert result is None

    def test_synthesized_metadata_has_correct_path(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Base-layer resolution produces correct full path in metadata."""
        mock_metadata.get.return_value = None

        result = resolver.resolve_read("/workspace/src/utils.py", overlay_config)

        assert result is not None
        assert result.path == "/workspace/src/utils.py"
        assert result.backend_name == "local"
        assert result.physical_path == "hash_utils"


# =============================================================================
# Whiteout Tests
# =============================================================================


class TestWhiteout:
    """Tests for whiteout creation and detection."""

    def test_is_whiteout_true(self, resolver: OverlayResolver) -> None:
        meta = _make_whiteout("/workspace/file.txt")
        assert resolver.is_whiteout(meta) is True

    def test_is_whiteout_false(self, resolver: OverlayResolver) -> None:
        meta = _make_file_meta("/workspace/file.txt", etag="real_hash")
        assert resolver.is_whiteout(meta) is False

    def test_create_whiteout(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Whiteout creation writes sentinel to metadata store."""
        resolver.create_whiteout("/workspace/README.md", overlay_config)

        mock_metadata.put.assert_called_once()
        written_meta = mock_metadata.put.call_args[0][0]
        assert written_meta.path == "/workspace/README.md"
        assert written_meta.etag == WHITEOUT_HASH
        assert written_meta.size == 0
        assert written_meta.backend_name == "overlay"
        assert written_meta.created_by == "agent-001"


# =============================================================================
# List Overlay Tests
# =============================================================================


class TestListOverlay:
    """Tests for list_overlay() — merging upper + base layers."""

    def test_merge_upper_and_base(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Upper and base entries are merged, upper takes precedence."""
        # Upper has modified main.py
        mock_metadata.list.return_value = [
            _make_file_meta("/workspace/src/main.py", etag="modified_hash", size=1500),
        ]

        result = resolver.list_overlay("/workspace/", overlay_config)

        paths = {m.path for m in result}
        # Should have: modified main.py from upper + utils.py and README.md from base
        assert "/workspace/src/main.py" in paths
        assert "/workspace/src/utils.py" in paths
        assert "/workspace/README.md" in paths
        assert len(result) == 3

        # Verify upper version wins
        main_meta = next(m for m in result if m.path == "/workspace/src/main.py")
        assert main_meta.etag == "modified_hash"

    def test_whiteouts_excluded(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Whiteout markers hide base files and are not in output."""
        mock_metadata.list.return_value = [
            _make_whiteout("/workspace/README.md"),
        ]

        result = resolver.list_overlay("/workspace/", overlay_config)

        paths = {m.path for m in result}
        assert "/workspace/README.md" not in paths
        assert "/workspace/src/main.py" in paths
        assert "/workspace/src/utils.py" in paths
        assert len(result) == 2

    def test_empty_upper_returns_all_base(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Empty upper layer returns all base files."""
        mock_metadata.list.return_value = []

        result = resolver.list_overlay("/workspace/", overlay_config)

        assert len(result) == 3

    def test_prefix_filtering(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Only entries matching prefix are included from base."""
        mock_metadata.list.return_value = []

        result = resolver.list_overlay("/workspace/src/", overlay_config)

        paths = {m.path for m in result}
        assert "/workspace/src/main.py" in paths
        assert "/workspace/src/utils.py" in paths
        assert "/workspace/README.md" not in paths
        assert len(result) == 2

    def test_disabled_overlay_delegates_to_metadata(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
    ) -> None:
        """Disabled overlay falls through to metadata.list()."""
        config = OverlayConfig(enabled=False)
        expected = [_make_file_meta("/workspace/file.txt")]
        mock_metadata.list.return_value = expected

        result = resolver.list_overlay("/workspace/", config)

        assert result == expected
        mock_metadata.list.assert_called_once_with(prefix="/workspace/")

    def test_upper_new_file_included(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """New file added only in upper layer appears in listing."""
        mock_metadata.list.return_value = [
            _make_file_meta("/workspace/new_file.txt", etag="new_hash"),
        ]

        result = resolver.list_overlay("/workspace/", overlay_config)

        paths = {m.path for m in result}
        assert "/workspace/new_file.txt" in paths
        assert len(result) == 4  # 3 base + 1 new


# =============================================================================
# Flatten Tests
# =============================================================================


class TestFlatten:
    """Tests for flatten() — merging upper into new manifest."""

    def test_flatten_with_modifications(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Flatten merges upper modifications into base."""
        mock_metadata.list.return_value = [
            _make_file_meta("/workspace/src/main.py", etag="modified_hash", size=1500),
        ]

        result = resolver.flatten(overlay_config)

        assert result.file_count == 3
        entry = result.get("src/main.py")
        assert entry is not None
        assert entry.content_hash == "modified_hash"
        assert entry.size == 1500

        # Base entries preserved
        assert result.get("src/utils.py") is not None
        assert result.get("README.md") is not None

    def test_flatten_with_whiteout(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Flatten removes files hidden by whiteouts."""
        mock_metadata.list.return_value = [
            _make_whiteout("/workspace/README.md"),
        ]

        result = resolver.flatten(overlay_config)

        assert result.file_count == 2
        assert result.get("README.md") is None
        assert result.get("src/main.py") is not None
        assert result.get("src/utils.py") is not None

    def test_flatten_with_new_file(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Flatten includes new files added in upper layer."""
        mock_metadata.list.return_value = [
            _make_file_meta(
                "/workspace/new_file.txt", etag="new_hash", size=300, mime_type="text/plain"
            ),
        ]

        result = resolver.flatten(overlay_config)

        assert result.file_count == 4
        new_entry = result.get("new_file.txt")
        assert new_entry is not None
        assert new_entry.content_hash == "new_hash"

    def test_flatten_clears_upper_layer(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Flatten calls delete_batch to clean up upper-layer entries."""
        mock_metadata.list.return_value = [
            _make_file_meta("/workspace/src/main.py", etag="modified_hash"),
            _make_whiteout("/workspace/README.md"),
        ]

        resolver.flatten(overlay_config)

        mock_metadata.delete_batch.assert_called_once()
        deleted_paths = mock_metadata.delete_batch.call_args[0][0]
        assert "/workspace/src/main.py" in deleted_paths
        assert "/workspace/README.md" in deleted_paths

    def test_flatten_empty_upper(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Flatten with empty upper returns base manifest unchanged."""
        mock_metadata.list.return_value = []

        result = resolver.flatten(overlay_config)

        assert result.file_count == 3
        assert result.get("src/main.py") is not None

    def test_flatten_disabled_raises(
        self,
        resolver: OverlayResolver,
    ) -> None:
        """Flatten raises ValueError when overlay is disabled."""
        config = OverlayConfig(enabled=False)
        with pytest.raises(ValueError, match="Cannot flatten"):
            resolver.flatten(config)

    def test_flatten_no_base_hash_raises(
        self,
        resolver: OverlayResolver,
    ) -> None:
        """Flatten raises ValueError when base_manifest_hash is None."""
        config = OverlayConfig(enabled=True, base_manifest_hash=None)
        with pytest.raises(ValueError, match="Cannot flatten"):
            resolver.flatten(config)


# =============================================================================
# Stats Tests
# =============================================================================


class TestOverlayStatsComputation:
    """Tests for overlay_stats() computation."""

    def test_all_base_files_shared(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """No upper modifications = 100% shared ratio."""
        mock_metadata.list.return_value = []

        stats = resolver.overlay_stats(overlay_config)

        assert stats.total_files == 3
        assert stats.base_files == 3
        assert stats.upper_files == 0
        assert stats.whiteout_count == 0
        assert stats.shared_ratio == 1.0
        assert stats.estimated_savings_bytes == 1700  # 1000 + 500 + 200

    def test_mixed_upper_and_base(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Mix of shared and modified files."""
        mock_metadata.list.return_value = [
            _make_file_meta("/workspace/src/main.py", etag="modified"),
        ]

        stats = resolver.overlay_stats(overlay_config)

        assert stats.total_files == 3  # 2 base + 1 upper
        assert stats.base_files == 2
        assert stats.upper_files == 1
        assert stats.shared_ratio == pytest.approx(2.0 / 3.0)

    def test_with_whiteouts(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Whiteouts reduce total count but show in stats."""
        mock_metadata.list.return_value = [
            _make_whiteout("/workspace/README.md"),
        ]

        stats = resolver.overlay_stats(overlay_config)

        assert stats.total_files == 2  # 2 base (README hidden)
        assert stats.base_files == 2
        assert stats.whiteout_count == 1
        assert stats.shared_ratio == 1.0  # 2/2

    def test_disabled_overlay_returns_empty_stats(
        self,
        resolver: OverlayResolver,
    ) -> None:
        """Disabled overlay returns zero stats."""
        config = OverlayConfig(enabled=False)
        stats = resolver.overlay_stats(config)
        assert stats.total_files == 0
        assert stats.shared_ratio == 0.0


# =============================================================================
# Cache Tests
# =============================================================================


class TestManifestCache:
    """Tests for manifest caching behavior."""

    def test_manifest_loaded_once(
        self,
        mock_metadata: MagicMock,
        mock_backend: MagicMock,
        base_manifest: WorkspaceManifest,
    ) -> None:
        """Manifest is fetched from backend only once, then cached."""
        mock_backend.read_content.return_value = MagicMock(
            unwrap=MagicMock(return_value=base_manifest.to_json())
        )
        resolver = OverlayResolver(metadata=mock_metadata, backend=mock_backend)

        # Load twice
        m1 = resolver.get_base_manifest("hash_1")
        m2 = resolver.get_base_manifest("hash_1")

        # Backend called only once
        mock_backend.read_content.assert_called_once_with("hash_1", context=None)
        assert m1 is m2  # Same object

    def test_different_hashes_cached_separately(
        self,
        mock_metadata: MagicMock,
        mock_backend: MagicMock,
    ) -> None:
        """Different base hashes get separate cache entries."""
        manifest1 = WorkspaceManifest(entries={"a.txt": ManifestEntry(content_hash="h1", size=10)})
        manifest2 = WorkspaceManifest(entries={"b.txt": ManifestEntry(content_hash="h2", size=20)})

        mock_backend.read_content.side_effect = [
            MagicMock(unwrap=MagicMock(return_value=manifest1.to_json())),
            MagicMock(unwrap=MagicMock(return_value=manifest2.to_json())),
        ]
        resolver = OverlayResolver(metadata=mock_metadata, backend=mock_backend)

        m1 = resolver.get_base_manifest("hash_1")
        m2 = resolver.get_base_manifest("hash_2")

        assert m1.file_count == 1
        assert m2.file_count == 1
        assert m1.get("a.txt") is not None
        assert m2.get("b.txt") is not None

    def test_clear_cache_specific(self, resolver: OverlayResolver) -> None:
        """clear_cache with hash removes only that entry."""
        resolver._manifest_cache["other_hash"] = WorkspaceManifest()
        resolver.clear_cache("manifest_hash_abc")
        assert "manifest_hash_abc" not in resolver._manifest_cache
        assert "other_hash" in resolver._manifest_cache

    def test_clear_cache_all(self, resolver: OverlayResolver) -> None:
        """clear_cache without hash clears everything."""
        resolver._manifest_cache["other_hash"] = WorkspaceManifest()
        resolver.clear_cache()
        assert len(resolver._manifest_cache) == 0


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Critical edge cases from design review."""

    def test_workspace_path_without_trailing_slash(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
    ) -> None:
        """Workspace path without trailing slash still works."""
        config = OverlayConfig(
            enabled=True,
            base_manifest_hash="manifest_hash_abc",
            workspace_path="/workspace",  # No trailing slash
        )
        mock_metadata.get.return_value = None

        result = resolver.resolve_read("/workspace/src/main.py", config)

        assert result is not None
        assert result.etag == "hash_main"

    def test_workspace_path_with_trailing_slash(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
    ) -> None:
        """Workspace path with trailing slash also works."""
        config = OverlayConfig(
            enabled=True,
            base_manifest_hash="manifest_hash_abc",
            workspace_path="/workspace/",  # With trailing slash
        )
        mock_metadata.get.return_value = None

        result = resolver.resolve_read("/workspace/src/main.py", config)

        assert result is not None
        assert result.etag == "hash_main"

    def test_file_at_workspace_root(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """File at workspace root (not in subdirectory) resolves correctly."""
        mock_metadata.get.return_value = None

        result = resolver.resolve_read("/workspace/README.md", overlay_config)

        assert result is not None
        assert result.etag == "hash_readme"

    def test_upper_whiteout_then_recreate(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """File deleted then recreated — upper has real entry, not whiteout."""
        # After delete+recreate, upper has a real file (not whiteout)
        mock_metadata.get.return_value = _make_file_meta(
            "/workspace/README.md", etag="recreated_hash", size=250
        )

        result = resolver.resolve_read("/workspace/README.md", overlay_config)

        assert result is not None
        assert not resolver.is_whiteout(result)
        assert result.etag == "recreated_hash"

    def test_multiple_agents_share_base(
        self,
        mock_metadata: MagicMock,
        mock_backend: MagicMock,
        base_manifest: WorkspaceManifest,
    ) -> None:
        """Two agents sharing same base hash use same cached manifest."""
        resolver = OverlayResolver(metadata=mock_metadata, backend=mock_backend)
        resolver._manifest_cache["shared_base"] = base_manifest

        config_a = OverlayConfig(
            enabled=True,
            base_manifest_hash="shared_base",
            workspace_path="/ws-a",
            agent_id="agent-a",
        )
        config_b = OverlayConfig(
            enabled=True,
            base_manifest_hash="shared_base",
            workspace_path="/ws-b",
            agent_id="agent-b",
        )

        # Both resolve through same cached manifest
        assert config_a.base_manifest_hash is not None
        assert config_b.base_manifest_hash is not None
        m1 = resolver.get_base_manifest(config_a.base_manifest_hash)
        m2 = resolver.get_base_manifest(config_b.base_manifest_hash)
        assert m1 is m2

    def test_empty_base_manifest(
        self,
        mock_metadata: MagicMock,
        mock_backend: MagicMock,
    ) -> None:
        """Overlay with empty base manifest works correctly."""
        resolver = OverlayResolver(metadata=mock_metadata, backend=mock_backend)
        resolver._manifest_cache["empty_hash"] = WorkspaceManifest()

        config = OverlayConfig(
            enabled=True,
            base_manifest_hash="empty_hash",
            workspace_path="/workspace",
        )
        mock_metadata.get.return_value = None

        result = resolver.resolve_read("/workspace/any_file.txt", config)
        assert result is None

    def test_flatten_produces_immutable_manifest(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
        base_manifest: WorkspaceManifest,
    ) -> None:
        """Flatten result is a new WorkspaceManifest, not a mutation of base."""
        mock_metadata.list.return_value = [
            _make_file_meta("/workspace/src/main.py", etag="modified"),
        ]

        result = resolver.flatten(overlay_config)

        # Original base manifest should be unchanged
        original_entry = base_manifest.get("src/main.py")
        assert original_entry is not None
        assert original_entry.content_hash == "hash_main"  # Unchanged

        # Flattened manifest has modification
        flattened_entry = result.get("src/main.py")
        assert flattened_entry is not None
        assert flattened_entry.content_hash == "modified"

    def test_upper_entry_without_etag_skipped_in_flatten(
        self,
        resolver: OverlayResolver,
        mock_metadata: MagicMock,
        overlay_config: OverlayConfig,
    ) -> None:
        """Upper entries without etag (e.g. directories) are skipped during flatten."""
        dir_meta = _make_file_meta("/workspace/src/", etag="", size=0)
        dir_meta.etag = None  # Directory has no etag
        mock_metadata.list.return_value = [dir_meta]

        result = resolver.flatten(overlay_config)

        # Should still have all 3 base entries (directory skipped)
        assert result.file_count == 3

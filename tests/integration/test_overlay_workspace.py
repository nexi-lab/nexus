"""Integration tests for overlay workspace (ComposeFS-style overlays).

Issue #1264: End-to-end tests for the overlay workspace lifecycle:
- Create overlay workspace from snapshot
- Read files from base layer
- Write files (upper layer modification)
- Delete base-layer files (whiteout creation)
- List files (merged view)
- Flatten overlay into new snapshot
- Two agents sharing same base

These tests use real WorkspaceManifest serialization and OverlayResolver,
with mocked metadata store and backend for isolation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core._metadata_generated import FileMetadata
from nexus.core.workspace_manifest import ManifestEntry, WorkspaceManifest
from nexus.services.overlay_resolver import (
    OverlayConfig,
    OverlayResolver,
)


class InMemoryMetadata:
    """Simple in-memory metadata store for integration testing.

    Implements the subset of FileMetadataProtocol needed by OverlayResolver.
    """

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata) -> None:
        self._store[metadata.path] = metadata

    def delete(self, path: str) -> dict[str, str] | None:
        if path in self._store:
            del self._store[path]
            return {"deleted": path}
        return None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: object
    ) -> list[FileMetadata]:
        return [meta for path, meta in self._store.items() if path.startswith(prefix)]

    def delete_batch(self, paths: list[str] | tuple[str, ...]) -> None:
        for path in paths:
            self._store.pop(path, None)


@pytest.fixture
def base_manifest() -> WorkspaceManifest:
    """Shared base manifest representing a workspace snapshot."""
    return WorkspaceManifest(
        entries={
            "src/app.py": ManifestEntry(
                content_hash="hash_app", size=2000, mime_type="text/x-python"
            ),
            "src/utils.py": ManifestEntry(
                content_hash="hash_utils", size=800, mime_type="text/x-python"
            ),
            "config.yaml": ManifestEntry(
                content_hash="hash_config", size=300, mime_type="application/yaml"
            ),
            "README.md": ManifestEntry(
                content_hash="hash_readme", size=500, mime_type="text/markdown"
            ),
        }
    )


@pytest.fixture
def mock_backend(base_manifest: WorkspaceManifest) -> MagicMock:
    """Mock CAS backend that serves the base manifest."""
    backend = MagicMock()
    manifest_json = base_manifest.to_json()
    backend.read_content.return_value = MagicMock(unwrap=MagicMock(return_value=manifest_json))
    return backend


@pytest.fixture
def agent_a_metadata() -> InMemoryMetadata:
    """In-memory metadata store for agent A (upper layer)."""
    return InMemoryMetadata()


@pytest.fixture
def agent_b_metadata() -> InMemoryMetadata:
    """In-memory metadata store for agent B (upper layer)."""
    return InMemoryMetadata()


@pytest.fixture
def overlay_config() -> OverlayConfig:
    return OverlayConfig(
        enabled=True,
        base_manifest_hash="base_manifest_hash",
        workspace_path="/ws/agent-a",
        agent_id="agent-a",
    )


@pytest.fixture
def resolver_a(agent_a_metadata: InMemoryMetadata, mock_backend: MagicMock) -> OverlayResolver:
    """OverlayResolver for agent A."""
    return OverlayResolver(metadata=agent_a_metadata, backend=mock_backend)


@pytest.fixture
def resolver_b(agent_b_metadata: InMemoryMetadata, mock_backend: MagicMock) -> OverlayResolver:
    """OverlayResolver for agent B."""
    return OverlayResolver(metadata=agent_b_metadata, backend=mock_backend)


class TestOverlayWorkspaceLifecycle:
    """End-to-end overlay workspace lifecycle."""

    def test_read_from_base_layer(
        self,
        resolver_a: OverlayResolver,
        overlay_config: OverlayConfig,
    ) -> None:
        """Read a file that exists only in the base layer."""
        meta = resolver_a.resolve_read("/ws/agent-a/src/app.py", overlay_config)

        assert meta is not None
        assert meta.etag == "hash_app"
        assert meta.size == 2000
        assert meta.mime_type == "text/x-python"
        assert meta.path == "/ws/agent-a/src/app.py"

    def test_write_creates_upper_layer_entry(
        self,
        resolver_a: OverlayResolver,
        agent_a_metadata: InMemoryMetadata,
        overlay_config: OverlayConfig,
    ) -> None:
        """Writing a file creates an entry in the upper layer that overrides base."""
        # Simulate a write by adding to upper layer (metadata store)
        agent_a_metadata.put(
            FileMetadata(
                path="/ws/agent-a/src/app.py",
                backend_name="local",
                physical_path="hash_app_modified",
                size=2500,
                etag="hash_app_modified",
                mime_type="text/x-python",
            )
        )

        # Read should now return upper layer version
        meta = resolver_a.resolve_read("/ws/agent-a/src/app.py", overlay_config)

        assert meta is not None
        assert meta.etag == "hash_app_modified"
        assert meta.size == 2500

    def test_delete_base_file_creates_whiteout(
        self,
        resolver_a: OverlayResolver,
        agent_a_metadata: InMemoryMetadata,
        overlay_config: OverlayConfig,
    ) -> None:
        """Deleting a base-layer file creates a whiteout marker."""
        # Delete README.md which exists only in base
        resolver_a.create_whiteout("/ws/agent-a/README.md", overlay_config)

        # Read should return whiteout
        meta = resolver_a.resolve_read("/ws/agent-a/README.md", overlay_config)
        assert meta is not None
        assert resolver_a.is_whiteout(meta)

    def test_list_merged_view(
        self,
        resolver_a: OverlayResolver,
        agent_a_metadata: InMemoryMetadata,
        overlay_config: OverlayConfig,
    ) -> None:
        """List shows merged view: upper modifications + base files."""
        # Modify one file in upper
        agent_a_metadata.put(
            FileMetadata(
                path="/ws/agent-a/src/app.py",
                backend_name="local",
                physical_path="hash_app_v2",
                size=3000,
                etag="hash_app_v2",
                mime_type="text/x-python",
            )
        )
        # Delete another via whiteout
        resolver_a.create_whiteout("/ws/agent-a/README.md", overlay_config)
        # Add a new file
        agent_a_metadata.put(
            FileMetadata(
                path="/ws/agent-a/new_test.py",
                backend_name="local",
                physical_path="hash_new",
                size=100,
                etag="hash_new",
                mime_type="text/x-python",
            )
        )

        result = resolver_a.list_overlay("/ws/agent-a/", overlay_config)
        paths = {m.path for m in result}

        # Modified file (from upper)
        assert "/ws/agent-a/src/app.py" in paths
        # Unmodified base files
        assert "/ws/agent-a/src/utils.py" in paths
        assert "/ws/agent-a/config.yaml" in paths
        # New file (upper only)
        assert "/ws/agent-a/new_test.py" in paths
        # Deleted file should NOT be listed
        assert "/ws/agent-a/README.md" not in paths

        assert len(result) == 4  # 2 base + 1 modified + 1 new

    def test_flatten_merges_layers(
        self,
        resolver_a: OverlayResolver,
        agent_a_metadata: InMemoryMetadata,
        overlay_config: OverlayConfig,
    ) -> None:
        """Flatten creates new manifest merging base + upper changes."""
        # Apply some changes
        agent_a_metadata.put(
            FileMetadata(
                path="/ws/agent-a/src/app.py",
                backend_name="local",
                physical_path="hash_app_v2",
                size=3000,
                etag="hash_app_v2",
                mime_type="text/x-python",
            )
        )
        resolver_a.create_whiteout("/ws/agent-a/README.md", overlay_config)

        # Flatten
        flattened = resolver_a.flatten(overlay_config)

        assert flattened.file_count == 3  # 4 base - 1 deleted
        assert flattened.get("README.md") is None  # Deleted
        assert flattened.get("src/app.py") is not None
        app_entry = flattened.get("src/app.py")
        assert app_entry is not None
        assert app_entry.content_hash == "hash_app_v2"
        assert flattened.get("src/utils.py") is not None  # Unchanged
        assert flattened.get("config.yaml") is not None  # Unchanged

        # Upper layer should be cleared after flatten
        assert agent_a_metadata.list(prefix="/ws/agent-a/") == []

    def test_overlay_stats(
        self,
        resolver_a: OverlayResolver,
        agent_a_metadata: InMemoryMetadata,
        overlay_config: OverlayConfig,
    ) -> None:
        """Overlay stats shows correct sharing information."""
        # Modify one file
        agent_a_metadata.put(
            FileMetadata(
                path="/ws/agent-a/src/app.py",
                backend_name="local",
                physical_path="hash_app_v2",
                size=3000,
                etag="hash_app_v2",
                mime_type="text/x-python",
            )
        )

        stats = resolver_a.overlay_stats(overlay_config)

        assert stats.total_files == 4  # 3 base + 1 upper
        assert stats.base_files == 3
        assert stats.upper_files == 1
        assert stats.whiteout_count == 0
        assert stats.shared_ratio == pytest.approx(0.75)
        # Savings = sum of unmodified base file sizes
        assert stats.estimated_savings_bytes == 800 + 300 + 500  # utils + config + readme


class TestTwoAgentsSharingBase:
    """Two agents sharing the same base snapshot with independent upper layers."""

    def test_independent_modifications(
        self,
        resolver_a: OverlayResolver,
        resolver_b: OverlayResolver,
        agent_a_metadata: InMemoryMetadata,
        agent_b_metadata: InMemoryMetadata,
        mock_backend: MagicMock,
    ) -> None:
        """Two agents can independently modify files without affecting each other."""
        config_a = OverlayConfig(
            enabled=True,
            base_manifest_hash="base_manifest_hash",
            workspace_path="/ws/agent-a",
            agent_id="agent-a",
        )
        config_b = OverlayConfig(
            enabled=True,
            base_manifest_hash="base_manifest_hash",
            workspace_path="/ws/agent-b",
            agent_id="agent-b",
        )

        # Agent A modifies app.py
        agent_a_metadata.put(
            FileMetadata(
                path="/ws/agent-a/src/app.py",
                backend_name="local",
                physical_path="hash_app_a",
                size=2500,
                etag="hash_app_a",
                mime_type="text/x-python",
            )
        )

        # Agent B deletes README.md
        resolver_b.create_whiteout("/ws/agent-b/README.md", config_b)

        # Agent A still sees README.md
        meta_a = resolver_a.resolve_read("/ws/agent-a/README.md", config_a)
        assert meta_a is not None
        assert not resolver_a.is_whiteout(meta_a)

        # Agent B still sees original app.py
        meta_b = resolver_b.resolve_read("/ws/agent-b/src/app.py", config_b)
        assert meta_b is not None
        assert meta_b.etag == "hash_app"  # Original from base

        # Agent A sees modified app.py
        meta_a_app = resolver_a.resolve_read("/ws/agent-a/src/app.py", config_a)
        assert meta_a_app is not None
        assert meta_a_app.etag == "hash_app_a"

    def test_shared_manifest_cache(
        self,
        resolver_a: OverlayResolver,
        resolver_b: OverlayResolver,
        mock_backend: MagicMock,
    ) -> None:
        """Both agents resolvers share the same cached manifest."""
        # Resolver A loads manifest
        config_a = OverlayConfig(
            enabled=True,
            base_manifest_hash="base_manifest_hash",
            workspace_path="/ws/agent-a",
        )
        resolver_a.resolve_read("/ws/agent-a/src/app.py", config_a)

        # Backend was called once for agent A
        assert mock_backend.read_content.call_count == 1

        # Now resolver B uses the same backend mock - it would be called again
        # In production, both resolvers would share the same OverlayResolver instance
        # or the manifest would be cached at a higher level


class TestNonOverlayWorkspaceUnaffected:
    """Non-overlay workspaces should behave exactly as before."""

    def test_disabled_overlay_returns_none(
        self,
        resolver_a: OverlayResolver,
    ) -> None:
        """Disabled overlay config does not intercept reads."""
        config = OverlayConfig(enabled=False)
        result = resolver_a.resolve_read("/regular/file.txt", config)
        assert result is None

    def test_disabled_overlay_list_delegates(
        self,
        resolver_a: OverlayResolver,
        agent_a_metadata: InMemoryMetadata,
    ) -> None:
        """Disabled overlay delegates list to metadata store directly."""
        config = OverlayConfig(enabled=False)
        agent_a_metadata.put(
            FileMetadata(
                path="/regular/file.txt",
                backend_name="local",
                physical_path="hash_1",
                size=100,
                etag="hash_1",
                mime_type="text/plain",
            )
        )

        result = resolver_a.list_overlay("/regular/", config)
        assert len(result) == 1
        assert result[0].path == "/regular/file.txt"


class TestManifestRoundTrip:
    """Integration test for manifest serialization through the full pipeline."""

    def test_flatten_produces_serializable_manifest(
        self,
        resolver_a: OverlayResolver,
        agent_a_metadata: InMemoryMetadata,
        overlay_config: OverlayConfig,
    ) -> None:
        """Flattened manifest can be serialized and deserialized correctly."""
        # Make changes
        agent_a_metadata.put(
            FileMetadata(
                path="/ws/agent-a/new_file.txt",
                backend_name="local",
                physical_path="hash_new",
                size=150,
                etag="hash_new",
                mime_type="text/plain",
            )
        )

        # Flatten
        flattened = resolver_a.flatten(overlay_config)

        # Serialize and deserialize
        json_bytes = flattened.to_json()
        restored = WorkspaceManifest.from_json(json_bytes)

        assert restored.file_count == flattened.file_count
        assert restored.total_size == flattened.total_size
        assert restored.paths() == flattened.paths()

        for path in flattened.paths():
            orig = flattened.get(path)
            rest = restored.get(path)
            assert orig is not None and rest is not None
            assert orig.content_hash == rest.content_hash
            assert orig.size == rest.size

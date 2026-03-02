"""Full-stack NexusFS integration tests for overlay workspace.

Issue #1264: Tests overlay resolution through the actual NexusFS kernel,
using real LocalBackend, real SQLAlchemyRecordStore, and real WorkspaceRegistry.
This verifies the overlay hooks in nexus_fs.py (read/delete) work
correctly when wired through the full dependency injection pipeline.

No Raft required — uses DictMetastore implementing MetastoreABC.
"""

import pytest

from nexus.backends.local import LocalBackend
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.workspace_manifest import ManifestEntry, WorkspaceManifest
from nexus.system_services.workspace.overlay_resolver import OverlayResolver
from tests.helpers.dict_metastore import DictMetastore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage_dir(tmp_path):
    """Real local storage directory."""
    d = tmp_path / "storage"
    d.mkdir()
    return d


@pytest.fixture
def local_backend(storage_dir) -> LocalBackend:
    """Real LocalBackend for CAS storage."""
    return LocalBackend(root_path=str(storage_dir))


@pytest.fixture
def metadata_store() -> DictMetastore:
    """In-memory metadata store (replaces Raft)."""
    return DictMetastore()


@pytest.fixture
def base_content() -> dict[str, bytes]:
    """Content for base layer files."""
    return {
        "src/app.py": b"def main():\n    print('hello')\n",
        "src/utils.py": b"def helper():\n    return 42\n",
        "config.yaml": b"debug: true\nport: 8080\n",
        "README.md": b"# My Project\nA sample project.\n",
    }


@pytest.fixture
def base_manifest(local_backend: LocalBackend, base_content: dict[str, bytes]) -> WorkspaceManifest:
    """Create a real base manifest with content stored in the CAS backend."""
    entries: dict[str, ManifestEntry] = {}
    for rel_path, content in base_content.items():
        result = local_backend.write_content(content)
        content_hash = result.content_hash
        entries[rel_path] = ManifestEntry(
            content_hash=content_hash,
            size=len(content),
            mime_type="text/plain",
        )
    return WorkspaceManifest(entries=entries)


@pytest.fixture
def stored_manifest_hash(local_backend: LocalBackend, base_manifest: WorkspaceManifest) -> str:
    """Store base manifest in CAS and return its hash."""
    manifest_json = base_manifest.to_json()
    result = local_backend.write_content(manifest_json)
    return result.content_hash


@pytest.fixture
def overlay_resolver(
    metadata_store: DictMetastore,
    local_backend: LocalBackend,
) -> OverlayResolver:
    """Real OverlayResolver wired to real backend and metadata store."""
    return OverlayResolver(metadata=metadata_store, backend=local_backend)


@pytest.fixture
def nexus_fs(
    local_backend: LocalBackend,
    metadata_store: DictMetastore,
    overlay_resolver: OverlayResolver,
    stored_manifest_hash: str,
):
    """Full NexusFS kernel with overlay resolver injected.

    Uses create_nexus_fs with real LocalBackend + SQLAlchemyRecordStore,
    then injects overlay_resolver post-construction (since factory doesn't
    propagate it yet — that's Phase 5 RPC wiring).
    """
    from nexus.core.config import PermissionConfig
    from nexus.factory import create_nexus_fs
    from nexus.storage.record_store import SQLAlchemyRecordStore

    record_store = SQLAlchemyRecordStore()  # in-memory SQLite

    nx = create_nexus_fs(
        backend=local_backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=False),
    )
    # Inject overlay resolver post-construction (NexusFS.__init__ accepts it,
    # but factory doesn't pass it through yet)
    nx._overlay_resolver = overlay_resolver

    # Ensure workspace_registry exists (factory may fail to create it if
    # ReBACManager is unavailable — rebac_manager is optional for the registry)
    if nx._workspace_registry is None:
        from nexus.bricks.workspace.workspace_registry import WorkspaceRegistry

        nx._workspace_registry = WorkspaceRegistry(
            metadata=metadata_store,
            rebac_manager=None,
            session_factory=record_store.session_factory,
        )

    # Register workspace with overlay config so _get_overlay_config() finds it
    nx._workspace_registry.register_workspace(
        path="/ws/agent-a",
        name="agent-a-workspace",
        metadata={
            "overlay_config": {
                "enabled": True,
                "base_manifest_hash": stored_manifest_hash,
                "agent_id": "agent-a",
            }
        },
    )

    yield nx
    nx.close()


# ---------------------------------------------------------------------------
# Tests: Full NexusFS read through overlay
# ---------------------------------------------------------------------------


class TestNexusFSOverlayRead:
    """Test NexusFS.read() with overlay resolution through the real kernel."""

    def test_read_file_from_base_layer(self, nexus_fs, base_content):
        """NexusFS.read() returns content from base layer when file not in upper."""
        content = nexus_fs.sys_read("/ws/agent-a/src/app.py")
        assert content == base_content["src/app.py"]

    def test_read_all_base_files(self, nexus_fs, base_content):
        """All base layer files are readable through NexusFS."""
        for rel_path, expected in base_content.items():
            content = nexus_fs.sys_read(f"/ws/agent-a/{rel_path}")
            assert content == expected, f"Mismatch for {rel_path}"

    def test_read_upper_layer_overrides_base(self, nexus_fs, metadata_store, local_backend):
        """File written to upper layer overrides base layer on read."""
        new_content = b"def main():\n    print('updated!')\n"
        new_hash = local_backend.write_content(new_content).content_hash

        # Write to upper layer (metadata store)
        metadata_store.put(
            FileMetadata(
                path="/ws/agent-a/src/app.py",
                backend_name="local",
                physical_path=new_hash,
                size=len(new_content),
                etag=new_hash,
                mime_type="text/x-python",
            )
        )

        # NexusFS should return upper layer content
        content = nexus_fs.sys_read("/ws/agent-a/src/app.py")
        assert content == new_content

    def test_read_nonexistent_file_raises(self, nexus_fs):
        """Reading a file that doesn't exist in base or upper raises error."""
        with pytest.raises(NexusFileNotFoundError):
            nexus_fs.sys_read("/ws/agent-a/nonexistent.py")

    def test_read_outside_overlay_workspace_raises(self, nexus_fs):
        """Reading outside the overlay workspace path raises error (no metadata)."""
        with pytest.raises(NexusFileNotFoundError):
            nexus_fs.sys_read("/other/path/file.txt")


# ---------------------------------------------------------------------------
# Tests: Full NexusFS write through overlay
# ---------------------------------------------------------------------------


class TestNexusFSOverlayWrite:
    """Test NexusFS.write() — writes go to upper layer naturally."""

    def test_write_new_file_in_overlay_workspace(self, nexus_fs):
        """Writing a new file creates an upper layer entry."""
        new_content = b"print('new file')\n"
        nexus_fs.sys_write("/ws/agent-a/new_module.py", new_content)

        # Should be readable back
        content = nexus_fs.sys_read("/ws/agent-a/new_module.py")
        assert content == new_content

    def test_write_overrides_base_file(self, nexus_fs, base_content):
        """Writing to a base-layer path creates upper entry that overrides base."""
        # First verify base content
        original = nexus_fs.sys_read("/ws/agent-a/config.yaml")
        assert original == base_content["config.yaml"]

        # Write new content
        new_content = b"debug: false\nport: 9090\n"
        nexus_fs.sys_write("/ws/agent-a/config.yaml", new_content)

        # Should now return new content
        content = nexus_fs.sys_read("/ws/agent-a/config.yaml")
        assert content == new_content

    def test_write_then_read_base_files_still_work(self, nexus_fs, base_content):
        """Writing one file doesn't affect other base layer files."""
        nexus_fs.sys_write("/ws/agent-a/config.yaml", b"updated config\n")

        # Other base files should still be readable
        assert nexus_fs.sys_read("/ws/agent-a/src/utils.py") == base_content["src/utils.py"]
        assert nexus_fs.sys_read("/ws/agent-a/README.md") == base_content["README.md"]


# ---------------------------------------------------------------------------
# Tests: Full NexusFS delete through overlay
# ---------------------------------------------------------------------------


class TestNexusFSOverlayDelete:
    """Test NexusFS.delete() with overlay whiteout creation."""

    def test_delete_base_file_creates_whiteout(self, nexus_fs):
        """Deleting a base-layer file creates a whiteout marker."""
        # File exists in base
        content = nexus_fs.sys_read("/ws/agent-a/README.md")
        assert content is not None

        # Delete it (should create whiteout)
        result = nexus_fs.sys_unlink("/ws/agent-a/README.md")
        assert result is not None
        assert result.get("overlay_whiteout") is True

        # Now reading should raise FileNotFoundError
        with pytest.raises(NexusFileNotFoundError):
            nexus_fs.sys_read("/ws/agent-a/README.md")

    def test_delete_upper_file_removes_normally(self, nexus_fs):
        """Deleting a file that exists only in upper layer removes it normally."""
        # Write a new file (upper only)
        nexus_fs.sys_write("/ws/agent-a/temp.py", b"temp content\n")
        assert nexus_fs.sys_read("/ws/agent-a/temp.py") == b"temp content\n"

        # Delete it — this is a normal delete (not whiteout)
        result = nexus_fs.sys_unlink("/ws/agent-a/temp.py")
        assert result is not None

        # Should no longer be readable
        with pytest.raises(NexusFileNotFoundError):
            nexus_fs.sys_read("/ws/agent-a/temp.py")


# ---------------------------------------------------------------------------
# Tests: Non-overlay path unaffected
# ---------------------------------------------------------------------------


class TestNonOverlayPathUnaffected:
    """Verify that paths outside the overlay workspace work normally."""

    def test_write_and_read_non_overlay_path(self, nexus_fs):
        """Paths outside the overlay workspace go through normal NexusFS flow."""
        nexus_fs.sys_write("/regular/test.txt", b"hello world\n")
        content = nexus_fs.sys_read("/regular/test.txt")
        assert content == b"hello world\n"

    def test_delete_non_overlay_path(self, nexus_fs):
        """Delete on non-overlay path works normally."""
        nexus_fs.sys_write("/regular/to_delete.txt", b"goodbye\n")
        result = nexus_fs.sys_unlink("/regular/to_delete.txt")
        assert result is not None
        assert "overlay_whiteout" not in (result or {})

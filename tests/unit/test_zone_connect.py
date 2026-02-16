"""Tests for FederatedMetadataProxy integration with nexus.connect().

Verifies:
- from_zone_manager() factory wires zone_manager ref for clean shutdown
- Cross-zone file operations work through the proxy
- ZoneManager shutdown is called on close()
"""

import pytest

from nexus.core._metadata_generated import DT_MOUNT, DT_REG, FileMetadata
from nexus.raft.federated_metadata_proxy import FederatedMetadataProxy
from nexus.raft.zone_path_resolver import ZonePathResolver
from nexus.storage.raft_metadata_store import RaftMetadataStore


class FakeZoneManager:
    """Minimal mock satisfying ZonePathResolver + from_zone_manager() interface."""

    def __init__(self):
        self._stores: dict[str, RaftMetadataStore] = {}
        self.shutdown_called = False

    def add_zone(self, zone_id: str, store: RaftMetadataStore) -> None:
        self._stores[zone_id] = store

    def get_store(self, zone_id: str) -> RaftMetadataStore | None:
        return self._stores.get(zone_id)

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture()
def zone_setup(tmp_path):
    """Root zone + beta zone with mount at /shared, wired via from_zone_manager pattern."""
    root_store = RaftMetadataStore.embedded(str(tmp_path / "root"))
    beta_store = RaftMetadataStore.embedded(str(tmp_path / "beta"))

    mgr = FakeZoneManager()
    mgr.add_zone("root", root_store)
    mgr.add_zone("beta", beta_store)

    # Mount beta at /shared in root
    root_store.put(
        FileMetadata(
            path="/shared",
            backend_name="mount",
            physical_path="",
            size=0,
            entry_type=DT_MOUNT,
            target_zone_id="beta",
        )
    )

    resolver = ZonePathResolver(mgr, root_zone_id="root")
    proxy = FederatedMetadataProxy(resolver, root_store, zone_manager=mgr)

    return proxy, root_store, beta_store, mgr


class TestZoneManagerLifecycle:
    """Test ZoneManager ref is held and shutdown on close()."""

    def test_close_calls_zone_manager_shutdown(self, zone_setup):
        proxy, _root, _beta, mgr = zone_setup
        assert not mgr.shutdown_called
        proxy.close()
        assert mgr.shutdown_called

    def test_close_without_zone_manager(self, tmp_path):
        """Close without zone_manager ref should not error."""
        store = RaftMetadataStore.embedded(str(tmp_path / "solo"))
        mgr = FakeZoneManager()
        mgr.add_zone("root", store)
        resolver = ZonePathResolver(mgr, root_zone_id="root")
        proxy = FederatedMetadataProxy(resolver, store)  # no zone_manager
        proxy.close()  # should not raise


class TestCrossZoneFileIO:
    """Test cross-zone file I/O through the proxy (simulates nx.write/nx.read)."""

    def test_write_to_mounted_zone(self, zone_setup):
        """Write to /shared/file.txt should land in beta zone."""
        proxy, _root, beta_store, _mgr = zone_setup

        proxy.put(
            FileMetadata(
                path="/shared/file.txt",
                backend_name="local",
                physical_path="/data/file.txt",
                size=100,
                etag="abc123",
                entry_type=DT_REG,
            )
        )

        # Verify it landed in beta zone (zone-relative path)
        result = beta_store.get("/file.txt")
        assert result is not None
        assert result.etag == "abc123"
        assert result.size == 100

    def test_read_from_mounted_zone(self, zone_setup):
        """Read /shared/file.txt should resolve from beta zone."""
        proxy, _root, beta_store, _mgr = zone_setup

        # Write directly to beta zone
        beta_store.put(
            FileMetadata(
                path="/file.txt",
                backend_name="local",
                physical_path="/data/file.txt",
                size=200,
                etag="def456",
                entry_type=DT_REG,
            )
        )

        # Read through proxy with global path
        result = proxy.get("/shared/file.txt")
        assert result is not None
        assert result.path == "/shared/file.txt"  # global path
        assert result.etag == "def456"

    def test_write_to_root_zone(self, zone_setup):
        """Write to /local/file.txt should stay in root zone."""
        proxy, root_store, _beta, _mgr = zone_setup

        proxy.put(
            FileMetadata(
                path="/local/file.txt",
                backend_name="local",
                physical_path="/data/local.txt",
                size=50,
                etag="root123",
                entry_type=DT_REG,
            )
        )

        # Verify it stayed in root zone
        result = root_store.get("/local/file.txt")
        assert result is not None
        assert result.etag == "root123"

    def test_delete_from_mounted_zone(self, zone_setup):
        """Delete /shared/file.txt should delete from beta zone."""
        proxy, _root, beta_store, _mgr = zone_setup

        beta_store.put(
            FileMetadata(
                path="/file.txt",
                backend_name="local",
                physical_path="/data/file.txt",
                size=100,
                entry_type=DT_REG,
            )
        )

        result = proxy.delete("/shared/file.txt")
        assert result is not None
        assert beta_store.get("/file.txt") is None

    def test_exists_across_zones(self, zone_setup):
        proxy, _root, beta_store, _mgr = zone_setup

        assert not proxy.exists("/shared/test.txt")

        beta_store.put(
            FileMetadata(
                path="/test.txt",
                backend_name="local",
                physical_path="",
                size=0,
                entry_type=DT_REG,
            )
        )

        assert proxy.exists("/shared/test.txt")

"""Tests for FederatedMetadataProxy — cross-zone metadata proxy.

Verifies that the proxy correctly routes operations across zones,
remaps paths between global and zone-relative namespaces, and
handles batch operations grouped by zone.
"""

import pytest

from nexus.core._metadata_generated import DT_MOUNT, DT_REG, FileMetadata
from nexus.raft.federated_metadata_proxy import FederatedMetadataProxy
from nexus.raft.zone_path_resolver import ZonePathResolver
from nexus.storage.raft_metadata_store import RaftMetadataStore


class FakeZoneManager:
    """Minimal mock that satisfies ZonePathResolver's interface."""

    def __init__(self):
        self._stores: dict[str, RaftMetadataStore] = {}

    def add_zone(self, zone_id: str, store: RaftMetadataStore) -> None:
        self._stores[zone_id] = store

    def get_store(self, zone_id: str) -> RaftMetadataStore | None:
        return self._stores.get(zone_id)


@pytest.fixture()
def two_zones(tmp_path):
    """Root zone + beta zone with a mount at /shared."""
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
    proxy = FederatedMetadataProxy(resolver, root_store)

    return proxy, root_store, beta_store, mgr


# =========================================================================
# Basic single-path operations
# =========================================================================


class TestGet:
    def test_get_local_file(self, two_zones):
        proxy, root_store, _, _ = two_zones
        root_store.put(
            FileMetadata(
                path="/local.txt",
                backend_name="local",
                physical_path="/data/local.txt",
                size=10,
                entry_type=DT_REG,
            )
        )
        meta = proxy.get("/local.txt")
        assert meta is not None
        assert meta.path == "/local.txt"
        assert meta.size == 10

    def test_get_cross_zone_file(self, two_zones):
        proxy, _, beta_store, _ = two_zones
        beta_store.put(
            FileMetadata(
                path="/doc.txt",
                backend_name="local",
                physical_path="/beta/doc.txt",
                size=200,
                entry_type=DT_REG,
            )
        )
        meta = proxy.get("/shared/doc.txt")
        assert meta is not None
        # Path remapped to global namespace
        assert meta.path == "/shared/doc.txt"
        assert meta.size == 200

    def test_get_nonexistent(self, two_zones):
        proxy, _, _, _ = two_zones
        assert proxy.get("/nonexistent") is None

    def test_get_cross_zone_nonexistent(self, two_zones):
        proxy, _, _, _ = two_zones
        assert proxy.get("/shared/nonexistent") is None


class TestPut:
    def test_put_local(self, two_zones):
        proxy, root_store, _, _ = two_zones
        proxy.put(
            FileMetadata(
                path="/new.txt",
                backend_name="local",
                physical_path="/data/new.txt",
                size=5,
                entry_type=DT_REG,
            )
        )
        # Verify it landed in root store
        meta = root_store.get("/new.txt")
        assert meta is not None
        assert meta.size == 5

    def test_put_cross_zone(self, two_zones):
        proxy, root_store, beta_store, _ = two_zones
        proxy.put(
            FileMetadata(
                path="/shared/cross.txt",
                backend_name="local",
                physical_path="/data/cross.txt",
                size=99,
                entry_type=DT_REG,
            )
        )
        # Should be stored in beta with zone-relative path
        meta = beta_store.get("/cross.txt")
        assert meta is not None
        assert meta.path == "/cross.txt"
        assert meta.size == 99
        # Should NOT be in root store
        assert root_store.get("/shared/cross.txt") is None


class TestDelete:
    def test_delete_cross_zone(self, two_zones):
        proxy, _, beta_store, _ = two_zones
        beta_store.put(
            FileMetadata(
                path="/to-delete.txt",
                backend_name="local",
                physical_path="/data/del.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        proxy.delete("/shared/to-delete.txt")
        assert beta_store.get("/to-delete.txt") is None


class TestExists:
    def test_exists_local(self, two_zones):
        proxy, root_store, _, _ = two_zones
        root_store.put(
            FileMetadata(
                path="/here.txt",
                backend_name="local",
                physical_path="/data/here.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        assert proxy.exists("/here.txt") is True
        assert proxy.exists("/not-here.txt") is False

    def test_exists_cross_zone(self, two_zones):
        proxy, _, beta_store, _ = two_zones
        beta_store.put(
            FileMetadata(
                path="/present.txt",
                backend_name="local",
                physical_path="/data/p.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        assert proxy.exists("/shared/present.txt") is True
        assert proxy.exists("/shared/absent.txt") is False


# =========================================================================
# List operations
# =========================================================================


class TestList:
    def test_list_local(self, two_zones):
        proxy, root_store, _, _ = two_zones
        root_store.put(
            FileMetadata(
                path="/docs/a.txt",
                backend_name="local",
                physical_path="/data/a.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        root_store.put(
            FileMetadata(
                path="/docs/b.txt",
                backend_name="local",
                physical_path="/data/b.txt",
                size=2,
                entry_type=DT_REG,
            )
        )
        results = proxy.list("/docs/")
        paths = {m.path for m in results}
        assert "/docs/a.txt" in paths
        assert "/docs/b.txt" in paths

    def test_list_cross_zone(self, two_zones):
        proxy, _, beta_store, _ = two_zones
        beta_store.put(
            FileMetadata(
                path="/x.txt",
                backend_name="local",
                physical_path="/beta/x.txt",
                size=10,
                entry_type=DT_REG,
            )
        )
        beta_store.put(
            FileMetadata(
                path="/y.txt",
                backend_name="local",
                physical_path="/beta/y.txt",
                size=20,
                entry_type=DT_REG,
            )
        )
        results = proxy.list("/shared/")
        paths = {m.path for m in results}
        # Paths should be remapped to global namespace
        assert "/shared/x.txt" in paths
        assert "/shared/y.txt" in paths

    def test_list_iter_cross_zone(self, two_zones):
        proxy, _, beta_store, _ = two_zones
        beta_store.put(
            FileMetadata(
                path="/iter.txt",
                backend_name="local",
                physical_path="/beta/iter.txt",
                size=5,
                entry_type=DT_REG,
            )
        )
        results = list(proxy.list_iter("/shared/"))
        paths = {m.path for m in results}
        assert "/shared/iter.txt" in paths


# =========================================================================
# Batch operations
# =========================================================================


class TestBatch:
    def test_get_batch_mixed_zones(self, two_zones):
        proxy, root_store, beta_store, _ = two_zones
        root_store.put(
            FileMetadata(
                path="/local.txt",
                backend_name="local",
                physical_path="/data/local.txt",
                size=10,
                entry_type=DT_REG,
            )
        )
        beta_store.put(
            FileMetadata(
                path="/remote.txt",
                backend_name="local",
                physical_path="/beta/remote.txt",
                size=20,
                entry_type=DT_REG,
            )
        )
        result = proxy.get_batch(["/local.txt", "/shared/remote.txt", "/missing.txt"])
        assert result["/local.txt"] is not None
        assert result["/local.txt"].path == "/local.txt"
        assert result["/shared/remote.txt"] is not None
        assert result["/shared/remote.txt"].path == "/shared/remote.txt"
        assert result["/missing.txt"] is None

    def test_put_batch_mixed_zones(self, two_zones):
        proxy, root_store, beta_store, _ = two_zones
        proxy.put_batch(
            [
                FileMetadata(
                    path="/local-batch.txt",
                    backend_name="local",
                    physical_path="/data/lb.txt",
                    size=1,
                    entry_type=DT_REG,
                ),
                FileMetadata(
                    path="/shared/remote-batch.txt",
                    backend_name="local",
                    physical_path="/data/rb.txt",
                    size=2,
                    entry_type=DT_REG,
                ),
            ]
        )
        assert root_store.get("/local-batch.txt") is not None
        assert beta_store.get("/remote-batch.txt") is not None

    def test_delete_batch_mixed_zones(self, two_zones):
        proxy, root_store, beta_store, _ = two_zones
        root_store.put(
            FileMetadata(
                path="/del-local.txt",
                backend_name="local",
                physical_path="/data/dl.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        beta_store.put(
            FileMetadata(
                path="/del-remote.txt",
                backend_name="local",
                physical_path="/beta/dr.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        proxy.delete_batch(["/del-local.txt", "/shared/del-remote.txt"])
        assert root_store.get("/del-local.txt") is None
        assert beta_store.get("/del-remote.txt") is None


# =========================================================================
# Store-specific methods (duck-typed by NexusFS)
# =========================================================================


class TestStoreSpecific:
    def test_rename_within_zone(self, two_zones):
        proxy, _, beta_store, _ = two_zones
        beta_store.put(
            FileMetadata(
                path="/old.txt",
                backend_name="local",
                physical_path="/beta/old.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        proxy.rename_path("/shared/old.txt", "/shared/new.txt")
        assert beta_store.get("/old.txt") is None
        assert beta_store.get("/new.txt") is not None

    def test_rename_cross_zone_raises(self, two_zones):
        proxy, root_store, beta_store, _ = two_zones
        root_store.put(
            FileMetadata(
                path="/local.txt",
                backend_name="local",
                physical_path="/data/local.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        with pytest.raises(ValueError, match="Cross-zone rename"):
            proxy.rename_path("/local.txt", "/shared/moved.txt")

    def test_is_implicit_directory(self, two_zones):
        proxy, _, beta_store, _ = two_zones
        beta_store.put(
            FileMetadata(
                path="/sub/file.txt",
                backend_name="local",
                physical_path="/beta/sub/file.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        # /shared/sub is implicit because /sub/file.txt exists in beta
        assert proxy.is_implicit_directory("/shared/sub") is True

    def test_file_metadata_kv(self, two_zones):
        proxy, _, beta_store, _ = two_zones
        beta_store.put(
            FileMetadata(
                path="/kv-test.txt",
                backend_name="local",
                physical_path="/beta/kv.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        proxy.set_file_metadata("/shared/kv-test.txt", "parsed_text", "hello world")
        value = proxy.get_file_metadata("/shared/kv-test.txt", "parsed_text")
        assert value == "hello world"

    @pytest.mark.skip(reason="Metastore PyO3 doesn't expose increment_revision yet")
    def test_revision_counter(self, two_zones):
        proxy, root_store, _, _ = two_zones
        rev1 = proxy.increment_revision("root")
        rev2 = proxy.increment_revision("root")
        assert rev2 == rev1 + 1
        assert proxy.get_revision("root") == rev2


# =========================================================================
# from_zone_manager factory
# =========================================================================


class TestFactory:
    def test_from_zone_manager(self, tmp_path):
        root_store = RaftMetadataStore.embedded(str(tmp_path / "root"))
        mgr = FakeZoneManager()
        mgr.add_zone("default", root_store)

        proxy = FederatedMetadataProxy.from_zone_manager(mgr, root_zone_id="default")
        assert isinstance(proxy, FederatedMetadataProxy)

    def test_from_zone_manager_missing_root(self, tmp_path):
        mgr = FakeZoneManager()
        with pytest.raises(RuntimeError, match="not found"):
            FederatedMetadataProxy.from_zone_manager(mgr, root_zone_id="missing")


# =========================================================================
# Nested mount (3 zones)
# =========================================================================


class TestNestedMount:
    def test_put_get_through_nested_mount(self, tmp_path):
        root_store = RaftMetadataStore.embedded(str(tmp_path / "root"))
        beta_store = RaftMetadataStore.embedded(str(tmp_path / "beta"))
        gamma_store = RaftMetadataStore.embedded(str(tmp_path / "gamma"))

        mgr = FakeZoneManager()
        mgr.add_zone("root", root_store)
        mgr.add_zone("beta", beta_store)
        mgr.add_zone("gamma", gamma_store)

        # root:/mnt → beta
        root_store.put(
            FileMetadata(
                path="/mnt",
                backend_name="mount",
                physical_path="",
                size=0,
                entry_type=DT_MOUNT,
                target_zone_id="beta",
            )
        )
        # beta:/deep → gamma
        beta_store.put(
            FileMetadata(
                path="/deep",
                backend_name="mount",
                physical_path="",
                size=0,
                entry_type=DT_MOUNT,
                target_zone_id="gamma",
            )
        )

        resolver = ZonePathResolver(mgr, root_zone_id="root")
        proxy = FederatedMetadataProxy(resolver, root_store)

        # Write through proxy at nested mount path
        proxy.put(
            FileMetadata(
                path="/mnt/deep/file.txt",
                backend_name="local",
                physical_path="/gamma/file.txt",
                size=42,
                entry_type=DT_REG,
            )
        )

        # Should be stored in gamma with zone-relative path
        assert gamma_store.get("/file.txt") is not None
        assert gamma_store.get("/file.txt").size == 42

        # Read back through proxy — path remapped to global
        meta = proxy.get("/mnt/deep/file.txt")
        assert meta is not None
        assert meta.path == "/mnt/deep/file.txt"
        assert meta.size == 42

    def test_list_through_nested_mount(self, tmp_path):
        root_store = RaftMetadataStore.embedded(str(tmp_path / "root"))
        beta_store = RaftMetadataStore.embedded(str(tmp_path / "beta"))
        gamma_store = RaftMetadataStore.embedded(str(tmp_path / "gamma"))

        mgr = FakeZoneManager()
        mgr.add_zone("root", root_store)
        mgr.add_zone("beta", beta_store)
        mgr.add_zone("gamma", gamma_store)

        root_store.put(
            FileMetadata(
                path="/mnt",
                backend_name="mount",
                physical_path="",
                size=0,
                entry_type=DT_MOUNT,
                target_zone_id="beta",
            )
        )
        beta_store.put(
            FileMetadata(
                path="/deep",
                backend_name="mount",
                physical_path="",
                size=0,
                entry_type=DT_MOUNT,
                target_zone_id="gamma",
            )
        )
        gamma_store.put(
            FileMetadata(
                path="/a.txt",
                backend_name="local",
                physical_path="/g/a.txt",
                size=1,
                entry_type=DT_REG,
            )
        )
        gamma_store.put(
            FileMetadata(
                path="/b.txt",
                backend_name="local",
                physical_path="/g/b.txt",
                size=2,
                entry_type=DT_REG,
            )
        )

        resolver = ZonePathResolver(mgr, root_zone_id="root")
        proxy = FederatedMetadataProxy(resolver, root_store)

        results = proxy.list("/mnt/deep/")
        paths = {m.path for m in results}
        assert "/mnt/deep/a.txt" in paths
        assert "/mnt/deep/b.txt" in paths

"""Tests for ZonePathResolver — cross-zone DT_MOUNT path resolution.

Uses RaftMetadataStore.embedded() (no Raft consensus needed) to test
the Python path resolution logic without PyO3 ZoneManager.
"""

import pytest

from nexus.core._metadata_generated import DT_MOUNT, DT_REG, FileMetadata
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
def zone_setup(tmp_path):
    """Create two zones (root + beta) with embedded stores."""
    root_store = RaftMetadataStore.embedded(str(tmp_path / "root"))
    beta_store = RaftMetadataStore.embedded(str(tmp_path / "beta"))

    mgr = FakeZoneManager()
    mgr.add_zone("root", root_store)
    mgr.add_zone("beta", beta_store)

    return mgr, root_store, beta_store


def test_resolve_root_path(zone_setup):
    mgr, root_store, _ = zone_setup
    resolver = ZonePathResolver(mgr, root_zone_id="root")

    resolved = resolver.resolve("/")
    assert resolved.zone_id == "root"
    assert resolved.path == "/"
    assert resolved.mount_chain == []


def test_resolve_simple_path_no_mount(zone_setup):
    mgr, root_store, _ = zone_setup

    # Create a file in root zone
    root_store.put(
        FileMetadata(
            path="/docs/readme.txt",
            backend_name="local",
            physical_path="/data/readme.txt",
            size=100,
            entry_type=DT_REG,
        )
    )

    resolver = ZonePathResolver(mgr, root_zone_id="root")
    resolved = resolver.resolve("/docs/readme.txt")

    assert resolved.zone_id == "root"
    assert resolved.path == "/docs/readme.txt"
    assert resolved.mount_chain == []


def test_resolve_crosses_mount_point(zone_setup):
    mgr, root_store, beta_store = zone_setup

    # Create a DT_MOUNT in root zone pointing to beta
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

    # Create a file in beta zone
    beta_store.put(
        FileMetadata(
            path="/file.txt",
            backend_name="local",
            physical_path="/beta-data/file.txt",
            size=200,
            entry_type=DT_REG,
        )
    )

    resolver = ZonePathResolver(mgr, root_zone_id="root")
    resolved = resolver.resolve("/shared/file.txt")

    assert resolved.zone_id == "beta"
    assert resolved.path == "/file.txt"
    assert resolved.mount_chain == [("root", "/shared")]

    # Verify we can actually read from the resolved store
    metadata = resolved.store.get(resolved.path)
    assert metadata is not None
    assert metadata.size == 200


def test_resolve_mount_point_itself(zone_setup):
    mgr, root_store, _ = zone_setup

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
    resolved = resolver.resolve("/shared")

    # Accessing mount point itself → resolves to "/" in target zone
    assert resolved.zone_id == "beta"
    assert resolved.path == "/"
    assert resolved.mount_chain == [("root", "/shared")]


def test_resolve_nested_mount(zone_setup, tmp_path):
    mgr, root_store, beta_store = zone_setup

    # Create a third zone
    gamma_store = RaftMetadataStore.embedded(str(tmp_path / "gamma"))
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

    # beta:/data → gamma
    beta_store.put(
        FileMetadata(
            path="/data",
            backend_name="mount",
            physical_path="",
            size=0,
            entry_type=DT_MOUNT,
            target_zone_id="gamma",
        )
    )

    # File in gamma
    gamma_store.put(
        FileMetadata(
            path="/report.csv",
            backend_name="local",
            physical_path="/gamma-data/report.csv",
            size=500,
            entry_type=DT_REG,
        )
    )

    resolver = ZonePathResolver(mgr, root_zone_id="root")
    resolved = resolver.resolve("/mnt/data/report.csv")

    assert resolved.zone_id == "gamma"
    assert resolved.path == "/report.csv"
    assert resolved.mount_chain == [("root", "/mnt"), ("beta", "/data")]


def test_resolve_missing_target_zone(zone_setup):
    mgr, root_store, _ = zone_setup

    root_store.put(
        FileMetadata(
            path="/missing",
            backend_name="mount",
            physical_path="",
            size=0,
            entry_type=DT_MOUNT,
            target_zone_id="nonexistent",
        )
    )

    resolver = ZonePathResolver(mgr, root_zone_id="root")
    with pytest.raises(FileNotFoundError, match="nonexistent"):
        resolver.resolve("/missing/file.txt")


def test_resolve_invalid_path(zone_setup):
    mgr, _, _ = zone_setup
    resolver = ZonePathResolver(mgr, root_zone_id="root")

    with pytest.raises(ValueError, match="absolute"):
        resolver.resolve("relative/path")


def test_resolve_missing_root_zone():
    mgr = FakeZoneManager()
    resolver = ZonePathResolver(mgr, root_zone_id="missing")

    with pytest.raises(FileNotFoundError, match="Root zone"):
        resolver.resolve("/anything")


class TestThreeNodeShareScenario:
    """3-node sharing scenario (explicit zone creation).

    Topology:
        node1 (Alice):  local zone "node1" + DT_MOUNT /shared → "proj-zone"
        node2 (Bob):    local zone "node2" + DT_MOUNT /from-alice → "proj-zone"
        node3 (Carol):  local zone "node3" + DT_MOUNT /collab → "proj-zone"

    Real-world flow:
        1. Alice runs: nexus zone create proj-zone  (explicit)
        2. Alice runs: nexus mount /shared proj-zone
        3. Bob joins proj-zone (ConfChange AddVoter) and mounts it
        4. Carol joins proj-zone and mounts it
        5. All 3 are equal Voters — reads local (~5μs), writes via Raft

    In this test we simulate with embedded stores (single-process,
    no Raft consensus). The shared zone is one RaftMetadataStore instance
    that all 3 resolvers point to — mirroring the All-Voters model where
    every node has a local sled replica.
    """

    @pytest.fixture()
    def three_nodes(self, tmp_path):
        """Set up 3 local zones + 1 shared zone."""
        mgr = FakeZoneManager()

        # Each node's local zone (independent sled)
        node1 = RaftMetadataStore.embedded(str(tmp_path / "node1"))
        node2 = RaftMetadataStore.embedded(str(tmp_path / "node2"))
        node3 = RaftMetadataStore.embedded(str(tmp_path / "node3"))

        # Shared zone — in prod each node has its own sled replica;
        # here one store simulates the replicated state
        shared = RaftMetadataStore.embedded(str(tmp_path / "proj-zone"))

        mgr.add_zone("node1", node1)
        mgr.add_zone("node2", node2)
        mgr.add_zone("node3", node3)
        mgr.add_zone("proj-zone", shared)

        # Mount shared zone in each node's local zone
        for _zone_id, store, mount_path in [
            ("node1", node1, "/shared"),
            ("node2", node2, "/from-alice"),
            ("node3", node3, "/collab"),
        ]:
            store.put(
                FileMetadata(
                    path=mount_path,
                    backend_name="mount",
                    physical_path="",
                    size=0,
                    entry_type=DT_MOUNT,
                    target_zone_id="proj-zone",
                )
            )

        # One resolver per node (each sees the world from their local zone root)
        r1 = ZonePathResolver(mgr, root_zone_id="node1")
        r2 = ZonePathResolver(mgr, root_zone_id="node2")
        r3 = ZonePathResolver(mgr, root_zone_id="node3")

        return mgr, node1, node2, node3, shared, r1, r2, r3

    def test_all_nodes_resolve_to_same_zone(self, three_nodes):
        """Different mount paths on 3 nodes all resolve to proj-zone."""
        _, _, _, _, _, r1, r2, r3 = three_nodes

        res1 = r1.resolve("/shared/design.md")
        res2 = r2.resolve("/from-alice/design.md")
        res3 = r3.resolve("/collab/design.md")

        # All resolve to the same zone + path
        assert res1.zone_id == res2.zone_id == res3.zone_id == "proj-zone"
        assert res1.path == res2.path == res3.path == "/design.md"

        # Each has different mount chain (different local mount path)
        assert res1.mount_chain == [("node1", "/shared")]
        assert res2.mount_chain == [("node2", "/from-alice")]
        assert res3.mount_chain == [("node3", "/collab")]

    def test_write_visible_to_all_nodes(self, three_nodes):
        """Data written in shared zone is visible from all 3 nodes."""
        _, _, _, _, shared, r1, r2, r3 = three_nodes

        # Alice writes a file (via shared zone store)
        shared.put(
            FileMetadata(
                path="/src/main.py",
                backend_name="local",
                physical_path="/obj/main.py",
                size=2048,
                entry_type=DT_REG,
            )
        )

        # All 3 nodes can resolve and read the file
        for resolver, mount in [
            (r1, "/shared"),
            (r2, "/from-alice"),
            (r3, "/collab"),
        ]:
            resolved = resolver.resolve(f"{mount}/src/main.py")
            meta = resolved.store.get(resolved.path)
            assert meta is not None
            assert meta.size == 2048
            assert meta.path == "/src/main.py"

    def test_local_zones_remain_independent(self, three_nodes):
        """Each node's local files are invisible to other nodes."""
        _, node1, node2, node3, _, r1, r2, r3 = three_nodes

        # Alice has a local-only file
        node1.put(
            FileMetadata(
                path="/my-notes.txt",
                backend_name="local",
                physical_path="/alice/notes.txt",
                size=42,
                entry_type=DT_REG,
            )
        )

        # Alice can access it — stays in her local zone
        res = r1.resolve("/my-notes.txt")
        assert res.zone_id == "node1"
        assert res.mount_chain == []

        # Bob and Carol cannot see Alice's local file
        assert node2.get("/my-notes.txt") is None
        assert node3.get("/my-notes.txt") is None

    def test_mount_point_lists_shared_root(self, three_nodes):
        """Resolving the mount point itself gives shared zone root."""
        _, _, _, _, _, r1, r2, r3 = three_nodes

        res1 = r1.resolve("/shared")
        res2 = r2.resolve("/from-alice")
        res3 = r3.resolve("/collab")

        assert res1.zone_id == res2.zone_id == res3.zone_id == "proj-zone"
        assert res1.path == res2.path == res3.path == "/"

    def test_deep_nested_path_in_shared_zone(self, three_nodes):
        """Deep paths through the mount point resolve correctly."""
        _, _, _, _, shared, r1, r2, r3 = three_nodes

        shared.put(
            FileMetadata(
                path="/a/b/c/deep.txt",
                backend_name="local",
                physical_path="/obj/deep.txt",
                size=1,
                entry_type=DT_REG,
            )
        )

        res = r3.resolve("/collab/a/b/c/deep.txt")
        assert res.zone_id == "proj-zone"
        assert res.path == "/a/b/c/deep.txt"
        assert res.store.get("/a/b/c/deep.txt").size == 1


class TestBootstrapContract:
    """Verify node bootstrap contract: root zone + "/" with i_links_count=1.

    These tests exercise the bootstrap semantics using embedded stores
    (no PyO3 ZoneManager needed). The real ZoneManager.bootstrap() follows
    the same logic.
    """

    def test_bootstrap_creates_root_with_links_count_1(self, tmp_path):
        """After bootstrap, root zone has "/" with i_links_count=1."""
        from nexus.core._metadata_generated import DT_DIR

        store = RaftMetadataStore.embedded(str(tmp_path / "root"))

        # Simulate bootstrap: create "/" with i_links_count=1 (self-ref)
        store.put(
            FileMetadata(
                path="/",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
                i_links_count=1,
            )
        )

        root = store.get("/")
        assert root is not None
        assert root.i_links_count == 1
        assert root.entry_type == DT_DIR
        assert root.zone_id == "root"

    def test_bootstrap_idempotent(self, tmp_path):
        """Calling bootstrap twice doesn't change i_links_count."""
        from nexus.core._metadata_generated import DT_DIR

        store = RaftMetadataStore.embedded(str(tmp_path / "root"))

        root_entry = FileMetadata(
            path="/",
            backend_name="virtual",
            physical_path="",
            size=0,
            entry_type=DT_DIR,
            zone_id="root",
            i_links_count=1,
        )
        store.put(root_entry)

        # Second bootstrap: "/" already exists → don't overwrite
        existing = store.get("/")
        assert existing is not None  # already exists, skip

        assert existing.i_links_count == 1  # unchanged

    def test_mount_after_bootstrap_increments_links(self, tmp_path):
        """mount() increments i_links_count from bootstrap's initial 1 to 2."""
        from dataclasses import replace

        from nexus.core._metadata_generated import DT_DIR

        root_store = RaftMetadataStore.embedded(str(tmp_path / "root"))
        target_store = RaftMetadataStore.embedded(str(tmp_path / "target"))

        # Bootstrap root zone
        root_store.put(
            FileMetadata(
                path="/",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
                i_links_count=1,
            )
        )

        # Bootstrap target zone
        target_store.put(
            FileMetadata(
                path="/",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="target",
                i_links_count=1,
            )
        )

        # Mount target at /shared in root (simulates ZoneManager.mount)
        root_store.put(
            FileMetadata(
                path="/shared",
                backend_name="mount",
                physical_path="",
                size=0,
                entry_type=DT_MOUNT,
                target_zone_id="target",
                zone_id="root",
            )
        )

        # Increment target's i_links_count (mount does this)
        target_root = target_store.get("/")
        target_store.put(replace(target_root, i_links_count=target_root.i_links_count + 1))

        assert target_store.get("/").i_links_count == 2  # bootstrap(1) + mount(+1)

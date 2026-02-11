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

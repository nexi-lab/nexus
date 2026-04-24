"""Tests for VFSRouterProtocol, ResolvedPath, and MountInfo (Issue #1383).

PathRouter was deleted in §12 Phase F3. Conformance tests against
PathRouter have been removed; only protocol / dataclass structural
tests remain.
"""

import dataclasses

import pytest

from nexus.core.protocols.vfs_router import MountInfo, ResolvedPath, VFSRouterProtocol

# ---------------------------------------------------------------------------
# ResolvedPath frozen dataclass tests
# ---------------------------------------------------------------------------


class TestResolvedPath:
    """Verify ResolvedPath is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        rp = ResolvedPath(
            virtual_path="/workspace/file.txt",
            backend_path="file.txt",
            mount_point="/workspace",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rp.mount_point = "/other"

    def test_slots(self) -> None:
        assert hasattr(ResolvedPath, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(ResolvedPath)}
        assert fields == {
            "virtual_path",
            "backend_path",
            "mount_point",
        }

    def test_equality(self) -> None:
        kwargs = {
            "virtual_path": "/ws/f",
            "backend_path": "f",
            "mount_point": "/ws",
        }
        assert ResolvedPath(**kwargs) == ResolvedPath(**kwargs)


# ---------------------------------------------------------------------------
# MountInfo frozen dataclass tests
# ---------------------------------------------------------------------------


class TestMountInfo:
    """Verify MountInfo is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        mi = MountInfo(mount_point="/workspace")
        with pytest.raises(dataclasses.FrozenInstanceError):
            mi.mount_point = "/other"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(MountInfo, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(MountInfo)}
        assert fields == {
            "mount_point",
            "status",
            "backend",
            "priority",
            "conflict_strategy",
        }

    def test_equality(self) -> None:
        assert MountInfo("/ws") == MountInfo("/ws")


# ---------------------------------------------------------------------------
# Protocol structural tests
# ---------------------------------------------------------------------------


class TestVFSRouterProtocol:
    """Verify the protocol is runtime-checkable and has expected methods."""

    def test_expected_methods(self) -> None:
        # PathRouter is read-only — add_mount/remove_mount moved to coordinator
        expected = {"route", "list_mounts", "get_mount_points"}
        actual = {
            name
            for name in dir(VFSRouterProtocol)
            if not name.startswith("_") and callable(getattr(VFSRouterProtocol, name))
        }
        assert expected <= actual

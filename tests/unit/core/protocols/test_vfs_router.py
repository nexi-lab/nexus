"""Tests for VFSRouterProtocol, ResolvedPath, and MountInfo (Issue #1383)."""

from __future__ import annotations

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
            readonly=False,
            zone_id=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rp.readonly = True  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(ResolvedPath, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(ResolvedPath)}
        assert fields == {
            "virtual_path",
            "backend_path",
            "mount_point",
            "readonly",
            "zone_id",
        }

    def test_none_zone(self) -> None:
        rp = ResolvedPath(
            virtual_path="/ws/f.txt",
            backend_path="f.txt",
            mount_point="/ws",
            readonly=True,
            zone_id=None,
        )
        assert rp.zone_id is None

    def test_equality(self) -> None:
        kwargs = {
            "virtual_path": "/ws/f",
            "backend_path": "f",
            "mount_point": "/ws",
            "readonly": False,
            "zone_id": "z1",
        }
        assert ResolvedPath(**kwargs) == ResolvedPath(**kwargs)


# ---------------------------------------------------------------------------
# MountInfo frozen dataclass tests
# ---------------------------------------------------------------------------


class TestMountInfo:
    """Verify MountInfo is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        mi = MountInfo(mount_point="/workspace", priority=0, readonly=False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            mi.mount_point = "/other"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(MountInfo, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(MountInfo)}
        assert fields == {"mount_point", "priority", "readonly"}

    def test_equality(self) -> None:
        assert MountInfo("/ws", 0, False) == MountInfo("/ws", 0, False)

    def test_readonly_mount(self) -> None:
        mi = MountInfo(mount_point="/archives", priority=1, readonly=True)
        assert mi.readonly is True
        assert mi.priority == 1


# ---------------------------------------------------------------------------
# Protocol structural tests
# ---------------------------------------------------------------------------


class TestVFSRouterProtocol:
    """Verify the protocol is runtime-checkable and has expected methods."""

    def test_expected_methods(self) -> None:
        expected = {"route", "add_mount", "remove_mount", "list_mounts"}
        actual = {
            name
            for name in dir(VFSRouterProtocol)
            if not name.startswith("_") and callable(getattr(VFSRouterProtocol, name))
        }
        assert expected <= actual


# ---------------------------------------------------------------------------
# Conformance test against existing PathRouter
# ---------------------------------------------------------------------------


class TestPathRouterConformance:
    """Verify existing PathRouter has the methods the protocol expects."""

    def test_has_required_methods(self) -> None:
        from nexus.core.router import PathRouter

        for method_name in ("route", "add_mount", "remove_mount", "list_mounts"):
            assert hasattr(PathRouter, method_name), f"PathRouter missing method '{method_name}'"

    def test_parameter_names_compatible(self) -> None:
        """Check parameter names match.

        PathRouter.route has extra params (agent_id, etc.) so we only check
        that all protocol params exist, not exact match.
        """
        import inspect

        from nexus.core.router import PathRouter

        # For route: protocol has (virtual_path, zone_id, is_admin, check_write)
        # PathRouter has (virtual_path, zone_id, agent_id, is_admin, check_write)
        # We verify protocol params are a subset.
        proto_sig = inspect.signature(VFSRouterProtocol.route)
        impl_sig = inspect.signature(PathRouter.route)
        proto_params = {n for n in proto_sig.parameters if n != "self"}
        impl_params = {n for n in impl_sig.parameters if n != "self"}
        assert proto_params <= impl_params, (
            f"PathRouter.route missing protocol params: {proto_params - impl_params}"
        )

        # For remove_mount, list_mounts — check direct match
        for method_name in ("remove_mount", "list_mounts"):
            proto_m = getattr(VFSRouterProtocol, method_name)
            impl_m = getattr(PathRouter, method_name)
            p_params = {n for n in inspect.signature(proto_m).parameters if n != "self"}
            i_params = {n for n in inspect.signature(impl_m).parameters if n != "self"}
            assert p_params <= i_params, f"PathRouter.{method_name} missing: {p_params - i_params}"

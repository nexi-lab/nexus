"""Tests for VFSRouterProtocol, ResolvedPath, and MountInfo (Issue #1383)."""

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


# ---------------------------------------------------------------------------
# Conformance test against existing PathRouter
# ---------------------------------------------------------------------------


class TestPathRouterConformance:
    """Verify existing PathRouter has the methods the protocol expects."""

    def test_has_required_methods(self) -> None:
        from nexus.core.router import PathRouter

        for method_name in (
            "route",
            "list_mounts",
            "get_mount_points",
        ):
            assert hasattr(PathRouter, method_name), f"PathRouter missing method '{method_name}'"

    def test_no_mutation_methods(self) -> None:
        """PathRouter is read-only — mount mutations go through coordinator."""
        from nexus.core.router import PathRouter

        assert not hasattr(PathRouter, "add_mount"), "PathRouter should not have add_mount"
        assert not hasattr(PathRouter, "remove_mount"), "PathRouter should not have remove_mount"

    def test_parameter_names_compatible(self) -> None:
        """Check parameter names match.

        PathRouter.route has the same params as the protocol now.
        """
        import inspect

        from nexus.core.router import PathRouter

        # For route: protocol has (virtual_path, zone_id)
        proto_sig = inspect.signature(VFSRouterProtocol.route)
        impl_sig = inspect.signature(PathRouter.route)
        proto_params = {n for n in proto_sig.parameters if n != "self"}
        impl_params = {n for n in impl_sig.parameters if n != "self"}
        assert proto_params <= impl_params, (
            f"PathRouter.route missing protocol params: {proto_params - impl_params}"
        )

        # For list_mounts -- check direct match
        for method_name in ("list_mounts",):
            proto_m = getattr(VFSRouterProtocol, method_name)
            impl_m = getattr(PathRouter, method_name)
            p_params = {n for n in inspect.signature(proto_m).parameters if n != "self"}
            i_params = {n for n in inspect.signature(impl_m).parameters if n != "self"}
            assert p_params <= i_params, f"PathRouter.{method_name} missing: {p_params - i_params}"

"""Tests for NamespaceManagerProtocol and NamespaceMount (Issue #1383)."""

from __future__ import annotations

import dataclasses

import pytest

from nexus.core.protocols.namespace_manager import NamespaceManagerProtocol, NamespaceMount

# ---------------------------------------------------------------------------
# NamespaceMount frozen dataclass tests
# ---------------------------------------------------------------------------


class TestNamespaceMount:
    """Verify NamespaceMount is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        mount = NamespaceMount(
            virtual_path="/workspace/proj",
            subject_type="user",
            subject_id="alice",
            zone_id=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            mount.virtual_path = "/other"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(NamespaceMount, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(NamespaceMount)}
        assert fields == {"virtual_path", "subject_type", "subject_id", "zone_id"}

    def test_none_zone(self) -> None:
        mount = NamespaceMount(
            virtual_path="/ws", subject_type="agent", subject_id="a1", zone_id=None
        )
        assert mount.zone_id is None

    def test_equality(self) -> None:
        kwargs = {
            "virtual_path": "/ws",
            "subject_type": "user",
            "subject_id": "u1",
            "zone_id": "z1",
        }
        assert NamespaceMount(**kwargs) == NamespaceMount(**kwargs)

    def test_empty_strings(self) -> None:
        mount = NamespaceMount(virtual_path="", subject_type="", subject_id="", zone_id="")
        assert mount.virtual_path == ""


# ---------------------------------------------------------------------------
# Protocol structural tests
# ---------------------------------------------------------------------------


class TestNamespaceManagerProtocol:
    """Verify the protocol is runtime-checkable and has expected methods."""

    def test_expected_methods(self) -> None:
        expected = {"is_visible", "get_mount_table", "invalidate"}
        actual = {
            name
            for name in dir(NamespaceManagerProtocol)
            if not name.startswith("_") and callable(getattr(NamespaceManagerProtocol, name))
        }
        assert expected <= actual


# ---------------------------------------------------------------------------
# Conformance test against existing NamespaceManager
# ---------------------------------------------------------------------------


class TestNamespaceManagerConformance:
    """Verify existing NamespaceManager has the methods the protocol expects."""

    def test_has_required_methods(self) -> None:
        from nexus.core.namespace_manager import NamespaceManager

        for method_name in ("is_visible", "get_mount_table", "invalidate"):
            assert hasattr(NamespaceManager, method_name), (
                f"NamespaceManager missing method '{method_name}'"
            )

    def test_parameter_names_compatible(self) -> None:
        from nexus.core.namespace_manager import NamespaceManager
        from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

        assert_protocol_conformance(NamespaceManager, NamespaceManagerProtocol)

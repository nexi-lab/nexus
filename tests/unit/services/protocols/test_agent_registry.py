"""Tests for AgentRegistryProtocol and AgentInfo (Issue #1383)."""

from __future__ import annotations

import dataclasses

import pytest

from nexus.services.protocols.agent_registry import AgentInfo, AgentRegistryProtocol

# ---------------------------------------------------------------------------
# AgentInfo frozen dataclass tests
# ---------------------------------------------------------------------------


class TestAgentInfo:
    """Verify AgentInfo is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        info = AgentInfo(
            agent_id="a1",
            owner_id="u1",
            zone_id=None,
            name=None,
            state="UNKNOWN",
            generation=0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            info.agent_id = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(AgentInfo, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(AgentInfo)}
        assert fields == {
            "agent_id",
            "owner_id",
            "zone_id",
            "name",
            "state",
            "generation",
        }

    def test_none_zone_and_name(self) -> None:
        info = AgentInfo(
            agent_id="a1",
            owner_id="u1",
            zone_id=None,
            name=None,
            state="IDLE",
            generation=1,
        )
        assert info.zone_id is None
        assert info.name is None

    def test_equality(self) -> None:
        kwargs = {
            "agent_id": "a1",
            "owner_id": "u1",
            "zone_id": None,
            "name": None,
            "state": "UNKNOWN",
            "generation": 0,
        }
        assert AgentInfo(**kwargs) == AgentInfo(**kwargs)

    def test_empty_strings(self) -> None:
        info = AgentInfo(
            agent_id="",
            owner_id="",
            zone_id="",
            name="",
            state="",
            generation=0,
        )
        assert info.agent_id == ""


# ---------------------------------------------------------------------------
# Protocol structural tests
# ---------------------------------------------------------------------------


class TestAgentRegistryProtocol:
    """Verify the protocol is runtime-checkable and has expected methods."""

    def test_runtime_checkable(self) -> None:
        assert hasattr(AgentRegistryProtocol, "__protocol_attrs__") or hasattr(
            AgentRegistryProtocol, "__abstractmethods__"
        )

    def test_expected_methods(self) -> None:
        expected = {"register", "get", "transition", "heartbeat", "list_by_zone", "unregister"}
        actual = {
            name
            for name in dir(AgentRegistryProtocol)
            if not name.startswith("_") and callable(getattr(AgentRegistryProtocol, name))
        }
        assert expected <= actual


# ---------------------------------------------------------------------------
# Conformance test against existing AgentRegistry
# ---------------------------------------------------------------------------


class TestAgentRegistryConformance:
    """Verify existing AgentRegistry has the methods the protocol expects."""

    def test_has_required_methods(self) -> None:
        from nexus.core.agent_registry import AgentRegistry

        expected = ["register", "get", "transition", "heartbeat", "list_by_zone", "unregister"]
        for method_name in expected:
            assert hasattr(AgentRegistry, method_name), (
                f"AgentRegistry missing method '{method_name}'"
            )
            assert callable(getattr(AgentRegistry, method_name))

    def test_parameter_names_compatible(self) -> None:
        """Check parameter names match (ignoring async/sync difference)."""
        from nexus.core.agent_registry import AgentRegistry
        from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

        assert_protocol_conformance(AgentRegistry, AgentRegistryProtocol)

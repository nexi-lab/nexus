"""Tests for AgentRuntimeRegistry — kernel-knows slot for in-process runtimes."""

from __future__ import annotations

import pytest

from nexus.services.sudo_code import AgentRuntime, AgentRuntimeRegistry


class _FakeRuntime:
    """Minimal AgentRuntime implementation for slot-mechanics tests."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.spawn_calls = 0
        self.cancel_calls = 0

    def spawn(self, *, pid, workspace_path, repos, model) -> None:  # noqa: D401
        self.spawn_calls += 1

    def cancel(self, *, pid, mode) -> None:  # noqa: D401
        self.cancel_calls += 1


class TestRegister:
    def test_register_then_get(self):
        rr = AgentRuntimeRegistry()
        rt = _FakeRuntime("scode-standard")
        rr.register("scode-standard", rt)
        assert rr.get("scode-standard") is rt

    def test_get_returns_none_for_unregistered_name(self):
        rr = AgentRuntimeRegistry()
        assert rr.get("scode-fast") is None

    def test_double_register_raises(self):
        rr = AgentRuntimeRegistry()
        rr.register("scode-standard", _FakeRuntime("a"))
        with pytest.raises(ValueError, match="already registered"):
            rr.register("scode-standard", _FakeRuntime("b"))

    def test_empty_agent_name_rejected(self):
        rr = AgentRuntimeRegistry()
        with pytest.raises(ValueError, match="agent_name is required"):
            rr.register("", _FakeRuntime("x"))


class TestUnregister:
    def test_unregister_removes_runtime(self):
        rr = AgentRuntimeRegistry()
        rr.register("scode-standard", _FakeRuntime("a"))
        rr.unregister("scode-standard")
        assert rr.get("scode-standard") is None

    def test_unregister_unknown_is_noop(self):
        """Test teardown should not have to track whether registration
        ever happened — unregister(unknown) is a clean no-op."""
        rr = AgentRuntimeRegistry()
        rr.unregister("never-registered")  # no exception
        rr.register("scode-standard", _FakeRuntime("a"))
        rr.unregister("scode-standard")
        rr.unregister("scode-standard")  # second unregister, also no-op


class TestList:
    def test_list_returns_registered_names(self):
        rr = AgentRuntimeRegistry()
        rr.register("scode-standard", _FakeRuntime("a"))
        rr.register("scode-fast", _FakeRuntime("b"))
        assert sorted(rr.list()) == ["scode-fast", "scode-standard"]

    def test_list_empty_registry(self):
        rr = AgentRuntimeRegistry()
        assert rr.list() == []


class TestProtocolShape:
    def test_fake_runtime_satisfies_runtime_protocol(self):
        """Sanity check: the in-test stub matches the AgentRuntime protocol
        so registry consumers can rely on isinstance(rt, AgentRuntime)."""
        rt = _FakeRuntime("scode-standard")
        assert isinstance(rt, AgentRuntime)

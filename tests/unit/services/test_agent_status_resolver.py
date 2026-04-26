"""Unit tests for AgentStatusResolver — procfs virtual filesystem.

Tests VFSPathResolver try_* conformance: try_read, try_write/try_delete rejection.
See: src/nexus/services/agents/agent_status_resolver.py
"""

import json

import pytest

from nexus.services.agents.agent_registry import AgentRegistry
from nexus.services.agents.agent_status_resolver import AgentStatusResolver

ZONE = "test-zone"
OWNER = "user-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resolver() -> tuple[AgentRegistry, AgentStatusResolver]:
    pt = AgentRegistry()
    return pt, AgentStatusResolver(pt)


# ---------------------------------------------------------------------------
# try_read()
# ---------------------------------------------------------------------------


class TestTryRead:
    def test_read_returns_json(self) -> None:
        pt, resolver = _make_resolver()
        desc = pt.spawn("agent-1", OWNER, ZONE)
        path = f"/{ZONE}/proc/{desc.pid}/status"

        content = resolver.try_read(path)
        assert isinstance(content, bytes)

        data = json.loads(content)
        assert data["pid"] == desc.pid
        assert data["name"] == "agent-1"
        assert data["owner_id"] == OWNER

    def test_read_returns_bytes(self) -> None:
        pt, resolver = _make_resolver()
        desc = pt.spawn("agent-1", OWNER, ZONE)
        path = f"/{ZONE}/proc/{desc.pid}/status"

        result = resolver.try_read(path)
        assert isinstance(result, bytes)
        data = json.loads(result)
        assert data["pid"] == desc.pid

    def test_read_nonexistent_returns_none(self) -> None:
        _, resolver = _make_resolver()
        result = resolver.try_read(f"/{ZONE}/proc/gone/status")
        assert result is None

    def test_read_wrong_path_returns_none(self) -> None:
        _, resolver = _make_resolver()
        assert resolver.try_read("/root/files/test.txt") is None
        assert resolver.try_read(f"/{ZONE}/proc/") is None
        assert resolver.try_read(f"/{ZONE}/proc/123") is None  # no /status

    def test_read_dynamic_from_memory(self) -> None:
        """Read reflects current in-memory state (not stale data)."""
        pt, resolver = _make_resolver()
        desc = pt.spawn("agent", OWNER, ZONE)
        path = f"/{ZONE}/proc/{desc.pid}/status"

        # First read
        content = resolver.try_read(path)
        assert content is not None

        # Kill the process — should no longer be readable
        pt.kill(desc.pid)
        assert resolver.try_read(path) is None


# ---------------------------------------------------------------------------
# try_write() / try_delete() — read-only
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_write_raises_on_proc_path(self) -> None:
        pt, resolver = _make_resolver()
        desc = pt.spawn("agent", OWNER, ZONE)
        with pytest.raises(PermissionError, match="read-only"):
            resolver.try_write(f"/{ZONE}/proc/{desc.pid}/status", b"data")

    def test_write_returns_none_on_non_proc_path(self) -> None:
        _, resolver = _make_resolver()
        assert resolver.try_write("/root/files/test.txt", b"data") is None

    def test_delete_raises_on_proc_path(self) -> None:
        pt, resolver = _make_resolver()
        desc = pt.spawn("agent", OWNER, ZONE)
        with pytest.raises(PermissionError, match="read-only"):
            resolver.try_delete(f"/{ZONE}/proc/{desc.pid}/status")

    def test_delete_returns_none_on_non_proc_path(self) -> None:
        _, resolver = _make_resolver()
        assert resolver.try_delete("/root/files/test.txt") is None

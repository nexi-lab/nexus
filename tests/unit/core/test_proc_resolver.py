"""Unit tests for ProcResolver — procfs virtual filesystem.

Tests VFSPathResolver try_* conformance: try_read, try_write/try_delete rejection.
See: src/nexus/system_services/proc/proc_resolver.py
"""

import json

import pytest

from nexus.core.agent_registry import AgentRegistry
from nexus.system_services.proc.proc_resolver import ProcResolver

ZONE = "test-zone"
OWNER = "user-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resolver() -> tuple[AgentRegistry, ProcResolver]:
    pt = AgentRegistry()
    return pt, ProcResolver(pt)


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

    def test_read_with_return_metadata(self) -> None:
        pt, resolver = _make_resolver()
        desc = pt.spawn("agent-1", OWNER, ZONE)
        path = f"/{ZONE}/proc/{desc.pid}/status"

        result = resolver.try_read(path, return_metadata=True)
        assert isinstance(result, dict)
        assert "content" in result
        assert "size" in result
        assert result["entry_type"] == 0  # DT_REG
        assert result["size"] == len(result["content"])

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

"""Memory client tests.

Verifies that .memory property returns a usable MemoryClient instance
and that all expected memory methods are present.

Issue #1289: Protocol + RPC Proxy pattern.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from nexus.remote.client import RemoteNexusFS
from nexus.remote.domain.memory import MemoryClient


@pytest.fixture
def remote_client() -> RemoteNexusFS:
    """Create a RemoteNexusFS with mocked HTTP transport."""
    with patch("nexus.remote.client.httpx.Client") as mock_client_class:
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        client = RemoteNexusFS(
            server_url="http://localhost:2026",
            timeout=30,
        )
        return client


class TestMemoryProperty:
    """Test .memory property."""

    def test_memory_returns_memory_client(self, remote_client: RemoteNexusFS) -> None:
        mem = remote_client.memory
        assert isinstance(mem, MemoryClient)

    def test_memory_is_cached(self, remote_client: RemoteNexusFS) -> None:
        mem1 = remote_client.memory
        mem2 = remote_client.memory
        assert mem1 is mem2


class TestMemoryClientMethods:
    """Test MemoryClient has all expected method names."""

    EXPECTED_METHODS = [
        "start_trajectory",
        "log_step",
        "log_trajectory_step",
        "complete_trajectory",
        "query_trajectories",
        "get_playbook",
        "query_playbooks",
        "process_relearning",
        "curate_playbook",
        "batch_reflect",
        "store",
        "list",
        "retrieve",
        "query",
        "search",
        "delete",
        "approve",
        "deactivate",
        "approve_batch",
        "deactivate_batch",
        "delete_batch",
    ]

    def test_all_methods_exist(self, remote_client: RemoteNexusFS) -> None:
        """MemoryClient exposes all expected methods."""
        mem = remote_client.memory
        for name in self.EXPECTED_METHODS:
            assert hasattr(mem, name), f"Missing method: {name}"
            assert callable(getattr(mem, name)), f"{name} is not callable"

    def test_method_count(self, remote_client: RemoteNexusFS) -> None:
        """Verify MemoryClient has at least 21 methods."""
        mem = remote_client.memory
        methods = [
            name for name in dir(mem) if not name.startswith("_") and callable(getattr(mem, name))
        ]
        assert len(methods) >= 21, f"Expected 21+ methods, found {len(methods)}: {methods}"

    def test_store_calls_rpc(self, remote_client: RemoteNexusFS) -> None:
        """Verify store() delegates to _call_rpc."""
        mem = remote_client.memory
        with patch.object(
            remote_client, "_call_rpc", return_value={"memory_id": "m-123"}
        ) as mock_rpc:
            result = mem.store("test content", memory_type="fact")
            assert result == "m-123"
            mock_rpc.assert_called_once()
            args = mock_rpc.call_args
            assert args[0][0] == "store_memory"

    def test_start_trajectory_calls_rpc(self, remote_client: RemoteNexusFS) -> None:
        """Verify start_trajectory() delegates to _call_rpc."""
        mem = remote_client.memory
        with patch.object(
            remote_client, "_call_rpc", return_value={"trajectory_id": "t-456"}
        ) as mock_rpc:
            result = mem.start_trajectory("test task")
            assert result == "t-456"
            mock_rpc.assert_called_once()
            args = mock_rpc.call_args
            assert args[0][0] == "start_trajectory"

    def test_delete_calls_rpc(self, remote_client: RemoteNexusFS) -> None:
        """Verify delete() delegates to _call_rpc."""
        mem = remote_client.memory
        with patch.object(remote_client, "_call_rpc", return_value={"deleted": True}) as mock_rpc:
            result = mem.delete("m-789")
            assert result is True
            mock_rpc.assert_called_once_with("delete_memory", {"memory_id": "m-789"})

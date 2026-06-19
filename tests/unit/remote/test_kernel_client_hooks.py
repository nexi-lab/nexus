from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from nexus.contracts.metadata import DT_DIR, DT_REG
from nexus.remote.kernel_client import (
    KernelClient,
    _find_free_port,
    _resolve_kernel_binary,
    _resolve_kernel_data_dir,
)


class RecordingHook:
    def __init__(self) -> None:
        self.pre: list[object] = []
        self.post: list[object] = []

    def on_pre_read(self, ctx: object) -> None:
        self.pre.append(ctx)

    def on_post_read(self, ctx: object) -> None:
        self.post.append(ctx)


def test_kernel_client_keeps_python_hook_registry_for_subprocess_mode() -> None:
    client = KernelClient(server_address="127.0.0.1:1")
    hook = RecordingHook()
    ctx = SimpleNamespace(path="/workspace/report.csv")

    client.register_hook("read", hook)

    assert client.hook_count("read") == 1
    client.dispatch_pre_hooks("read", ctx)
    client.dispatch_post_hooks("read", ctx)

    assert hook.pre == [ctx]
    assert hook.post == [ctx]
    assert client.unregister_hook("read", hook) is True
    assert client.hook_count("read") == 0


def test_kernel_client_resolves_cargo_binary_name_without_symlink(monkeypatch) -> None:
    monkeypatch.delenv("NEXUS_KERNEL_BINARY", raising=False)

    def fake_which(binary_name: str) -> str | None:
        if binary_name == "nexusd-cluster":
            return "/repo/target/debug/nexusd-cluster"
        return None

    with patch("nexus.remote.kernel_client.shutil.which", side_effect=fake_which):
        assert _resolve_kernel_binary() == "/repo/target/debug/nexusd-cluster"


def test_kernel_client_routes_legacy_metadata_file_to_sidecar_data_dir(tmp_path) -> None:
    legacy_db = tmp_path / "nexus.db"
    legacy_db.write_text("legacy metadata")

    assert _resolve_kernel_data_dir(str(legacy_db)) == str(tmp_path / "nexus.db.kernel")


def test_kernel_client_keeps_directory_metadata_path(tmp_path) -> None:
    metadata_dir = tmp_path / "metastore"
    metadata_dir.mkdir()

    assert _resolve_kernel_data_dir(str(metadata_dir)) == str(metadata_dir)


def test_kernel_client_port_selection_avoids_reserved_vfs_grpc_port(monkeypatch) -> None:
    ports = iter([2028, 2126])

    class FakeSocket:
        def __enter__(self) -> "FakeSocket":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def bind(self, _addr: tuple[str, int]) -> None:
            return None

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", next(ports))

    monkeypatch.setenv("NEXUS_GRPC_PORT", "2028")
    monkeypatch.setattr("socket.socket", lambda *_args, **_kwargs: FakeSocket())

    assert _find_free_port() == 2126


def test_kernel_client_sys_read_raw_returns_bytes() -> None:
    class FakeTransport:
        def read_file(self, *_args: object, **_kwargs: object) -> bytes:
            return b"raw-content"

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    assert client.sys_read_raw("/workspace/file.txt", "root") == b"raw-content"


def test_kernel_client_agent_registry_is_property_proxy() -> None:
    client = KernelClient(server_address="127.0.0.1:1")

    assert hasattr(client.agent_registry, "register_external")


def test_kernel_client_agent_registry_proxy_wraps_external_lifecycle_calls() -> None:
    class FakeTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def call_rpc(
            self,
            method: str,
            params: dict[str, object],
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            self.calls.append((method, params))
            if method == "agent_register_external":
                return {
                    "pid": params["connection_id"],
                    "name": params["name"],
                    "kind": "UNMANAGED",
                    "owner_id": params["owner_id"],
                    "zone_id": params["zone_id"],
                    "state": "REGISTERED",
                    "generation": 1,
                    "created_at_ms": 1_700_000_000_000,
                    "updated_at_ms": 1_700_000_000_000,
                    "external_info": {"connection_id": params["connection_id"]},
                    "labels": {"capabilities": "search,cache"},
                }
            if method == "agent_signal":
                return {
                    "pid": params["pid"],
                    "name": "agent",
                    "kind": "UNMANAGED",
                    "owner_id": "admin",
                    "zone_id": "root",
                    "state": "SUSPENDED",
                    "generation": 1,
                    "created_at_ms": 1_700_000_000_000,
                    "updated_at_ms": 1_700_000_000_001,
                    "external_info": {"connection_id": params["pid"]},
                }
            if method == "agent_update_state":
                return {
                    "pid": params["pid"],
                    "name": "agent",
                    "kind": "UNMANAGED",
                    "owner_id": "admin",
                    "zone_id": "root",
                    "state": "WARMING_UP",
                    "generation": 1,
                    "created_at_ms": 1_700_000_000_000,
                    "updated_at_ms": 1_700_000_000_001,
                    "external_info": {"connection_id": params["pid"]},
                }
            if method == "agent_list":
                return []
            return None

    from nexus.contracts.process_types import AgentSignal, AgentState

    client = KernelClient(server_address="127.0.0.1:1")
    transport = FakeTransport()
    client._transport = transport

    desc = client.agent_registry.register_external(
        name="agent",
        owner_id="admin",
        zone_id="root",
        connection_id="admin,agent",
    )
    transitioned = client.agent_registry.signal("admin,agent", AgentSignal.SIGSTOP)
    warming = client.agent_registry.update_state("admin,agent", AgentState.WARMING_UP.value)
    listed = client.agent_registry.list_processes(zone_id="root", state=AgentState.SUSPENDED)

    assert desc.pid == "admin,agent"
    assert desc.state == AgentState.REGISTERED
    assert desc.capabilities == ["search", "cache"]
    assert transitioned.state == AgentState.SUSPENDED
    assert warming.state == AgentState.WARMING_UP
    assert listed == []
    assert transport.calls == [
        (
            "agent_register_external",
            {
                "name": "agent",
                "owner_id": "admin",
                "zone_id": "root",
                "connection_id": "admin,agent",
                "host_pid": None,
                "remote_addr": None,
                "protocol": "grpc",
                "parent_pid": None,
                "labels": {},
            },
        ),
        ("agent_signal", {"pid": "admin,agent", "sig": "SIGSTOP", "payload": {}}),
        ("agent_update_state", {"pid": "admin,agent", "state": "warming_up"}),
        (
            "agent_list",
            {"zone_id": "root", "owner_id": None, "kind": None, "state": "suspended"},
        ),
    ]


def test_kernel_client_sys_read_threads_timeout_and_offset_through_typed_read() -> None:
    """sys_read goes through the typed Read RPC with timeout_ms/offset on the wire.

    Replaces the prior Call("sys_read", …) fallback path. Asserts the
    proto-extension contract: the typed handler reads timeout_ms / offset
    from the request and returns the full pipe/stream fields.
    """

    class FakeResponse:
        def __init__(self) -> None:
            self.content = b""
            self.content_id = ""
            self.gen = 0
            self.entry_type = 3
            self.post_hook_needed = False

        @staticmethod
        def HasField(_name: str) -> bool:
            return False

    class FakeTransport:
        def __init__(self) -> None:
            self.read_calls: list[dict[str, object]] = []

        def read(self, path: str, **kwargs: object) -> FakeResponse:
            self.read_calls.append({"path": path, **kwargs})
            return FakeResponse()

    client = KernelClient(server_address="127.0.0.1:1")
    transport = FakeTransport()
    client._transport = transport

    result = client.sys_read("/nexus/pipes/task-dispatch", timeout_ms=0, offset=42)

    assert result.data == b""
    assert result.entry_type == 3
    assert transport.read_calls == [
        {
            "path": "/nexus/pipes/task-dispatch",
            "content_id": "",
            "timeout_ms": 0,
            "offset": 42,
            "read_timeout": client._timeout,
        }
    ]


def test_agent_registry_proxy_count_by_state_delegates_to_agent_list() -> None:
    """_AgentRegistryProxy.count_by_state() counts agents returned by agent_list with state filter."""
    from nexus.contracts.process_types import AgentState

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                assert params["state"] == "busy"
                return [
                    {"pid": "a1", "state": "busy", "kind": "unmanaged", "labels": {}},
                    {"pid": "a2", "state": "busy", "kind": "unmanaged", "labels": {}},
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    count = client.agent_registry.count_by_state(AgentState.BUSY)

    assert count == 2


def test_agent_registry_proxy_list_by_priority_returns_busy_agents_up_to_batch_size() -> None:
    """_AgentRegistryProxy.list_by_priority() returns BUSY agents limited to batch_size."""
    from nexus.contracts.process_types import AgentState

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                assert params["state"] == "busy"
                return [
                    {"pid": f"a{i}", "state": "busy", "kind": "unmanaged", "labels": {}}
                    for i in range(5)
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    result = client.agent_registry.list_by_priority(batch_size=3)

    assert len(result) == 3
    assert all(hasattr(a, "state") for a in result)
    assert all(a.state is AgentState.BUSY for a in result)


def test_agent_registry_proxy_list_by_priority_orders_by_lru_updated_at() -> None:
    """list_by_priority returns oldest-updated BUSY agents first (LRU), matching the
    Rust kernel SSOT (registry.rs: sort_by_key(updated_at_ms))."""

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                # Returned out of LRU order on purpose.
                return [
                    {
                        "pid": "newest",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {},
                        "updated_at_ms": 3000,
                    },
                    {
                        "pid": "oldest",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {},
                        "updated_at_ms": 1000,
                    },
                    {
                        "pid": "middle",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {},
                        "updated_at_ms": 2000,
                    },
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    result = client.agent_registry.list_by_priority(batch_size=2)

    # Oldest-updated first, capped to batch_size → drops the newest.
    assert [a.pid for a in result] == ["oldest", "middle"]


def test_agent_registry_proxy_list_by_priority_orders_by_eviction_priority_then_lru() -> None:
    """list_by_priority sorts by (eviction_priority ASC, updated_at_ms ASC), exactly
    mirroring the Rust kernel SSOT (registry.rs list_by_priority test, prio 10/30/20)."""

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                return [
                    {
                        "pid": "p30",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "30"},
                        "updated_at_ms": 100,
                    },
                    {
                        "pid": "p10",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "10"},
                        "updated_at_ms": 999,
                    },
                    {
                        "pid": "p20",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "20"},
                        "updated_at_ms": 100,
                    },
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    result = client.agent_registry.list_by_priority(batch_size=2)

    # Lowest eviction_priority first (10, 20) — NOT by updated_at_ms, which would
    # have put the newest-updated p10 last.
    assert [a.pid for a in result] == ["p10", "p20"]


def test_agent_registry_proxy_list_by_priority_defaults_missing_priority_to_50() -> None:
    """Agents without an eviction_priority label default to 50 (Rust SSOT unwrap_or(50)),
    so an explicit lower priority is evicted before an unlabelled one."""

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                return [
                    {
                        "pid": "unlabelled",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {},
                        "updated_at_ms": 1,
                    },
                    {
                        "pid": "low",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "10"},
                        "updated_at_ms": 999,
                    },
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    result = client.agent_registry.list_by_priority(batch_size=2)

    # low (10) sorts before unlabelled (default 50) despite being newer-updated.
    assert [a.pid for a in result] == ["low", "unlabelled"]


def test_eviction_cycle_runs_against_proxy_wired_manager() -> None:
    """One full EvictionManager.run_cycle() against a real _AgentRegistryProxy.

    Regression for issue #4268: the proxy must expose count_by_state +
    list_by_priority so the background eviction task does not crash. Drives
    the whole pipeline (count → list_by_priority → get/CAS → signal) through
    the gRPC proxy surface, not a MagicMock.
    """
    import asyncio
    from unittest.mock import AsyncMock

    from nexus.contracts.agent_types import EvictionReason
    from nexus.contracts.process_types import AgentState
    from nexus.lib.performance_tuning import EvictionTuning
    from nexus.services.agents.eviction_manager import EvictionManager
    from nexus.services.agents.eviction_policy import LRUEvictionPolicy
    from nexus.services.agents.resource_monitor import PressureLevel

    agents = {
        "a1": {
            "pid": "a1",
            "state": "busy",
            "kind": "unmanaged",
            "labels": {},
            "generation": 1,
            "updated_at_ms": 1000,
        },
        "a2": {
            "pid": "a2",
            "state": "busy",
            "kind": "unmanaged",
            "labels": {},
            "generation": 1,
            "updated_at_ms": 2000,
        },
    }

    class FakeTransport:
        def __init__(self) -> None:
            self.signalled: list[str] = []

        def call_rpc(
            self,
            method: str,
            params: dict[str, object],
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                return list(agents.values())
            if method == "agent_get":
                return agents.get(params["pid"])
            if method == "agent_signal":
                self.signalled.append(str(params["pid"]))
                return agents.get(params["pid"])
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    transport = FakeTransport()
    client._transport = transport

    monitor = AsyncMock()
    monitor.check_pressure.return_value = PressureLevel.CRITICAL

    manager = EvictionManager(
        agent_registry=client.agent_registry,
        monitor=monitor,
        policy=LRUEvictionPolicy(),
        tuning=EvictionTuning(
            memory_high_watermark_pct=85,
            memory_low_watermark_pct=75,
            max_active_agents=100,
            eviction_batch_size=5,
            checkpoint_timeout_seconds=5.0,
            eviction_cooldown_seconds=60,
            max_concurrent_transitions=10,
        ),
    )

    result = asyncio.run(manager.run_cycle())

    assert result.reason is EvictionReason.PRESSURE_CRITICAL
    assert result.evicted == 2
    assert sorted(transport.signalled) == ["a1", "a2"]
    # count_by_state delegates through the proxy without raising.
    assert client.agent_registry.count_by_state(AgentState.BUSY) == 2


def test_agent_registry_proxy_descriptor_exposes_datetimes_and_external_info() -> None:
    """Proxy descriptors must duck-type as AgentDescriptor for the eviction policy:
    .updated_at/.created_at datetimes, .external_info.last_heartbeat datetime, .labels dict."""
    from datetime import datetime

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_get":
                return {
                    "pid": "a1",
                    "state": "busy",
                    "kind": "unmanaged",
                    "owner_id": "u",
                    "zone_id": "z",
                    "generation": 1,
                    "created_at_ms": 1_700_000_000_000,
                    "updated_at_ms": 1_700_000_005_000,
                    "labels": {"eviction_class": "spot"},
                    "external_info": {
                        "connection_id": "c1",
                        "last_heartbeat_ms": 1_700_000_009_000,
                    },
                }
            if method == "agent_heartbeat":
                # No external_info / timestamps on the wire → must not raise on access.
                return {"pid": "a2", "state": "busy", "kind": "unmanaged", "labels": {}}
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    desc = client.agent_registry.get("a1")
    assert isinstance(desc.updated_at, datetime)
    assert isinstance(desc.created_at, datetime)
    assert isinstance(desc.labels, dict)
    assert desc.external_info is not None
    assert isinstance(desc.external_info.last_heartbeat, datetime)
    assert desc.external_info.connection_id == "c1"

    # Missing external_info / timestamps degrade to None, never AttributeError.
    sparse = client.agent_registry.heartbeat("a2")
    assert sparse.external_info is None
    assert sparse.updated_at is None
    assert isinstance(sparse.labels, dict)


def test_eviction_cycle_runs_against_proxy_wired_qos_manager() -> None:
    """Full EvictionManager.run_cycle() with the PRODUCTION-wired QoSEvictionPolicy
    against a real _AgentRegistryProxy.

    Regression for issue #4268: app_state wires QoSEvictionPolicy unconditionally,
    whose select_candidates reads p.external_info.last_heartbeat / p.updated_at /
    p.labels. Proxy descriptors must expose those or the eviction cycle still
    crashes (the second AttributeError after count_by_state/list_by_priority).
    """
    import asyncio
    from unittest.mock import AsyncMock

    from nexus.contracts.agent_types import EvictionReason
    from nexus.lib.performance_tuning import EvictionTuning
    from nexus.services.agents.eviction_manager import EvictionManager
    from nexus.services.agents.eviction_policy import QoSEvictionPolicy
    from nexus.services.agents.resource_monitor import PressureLevel

    agents = {
        "spot-1": {
            "pid": "spot-1",
            "state": "busy",
            "kind": "unmanaged",
            "labels": {"eviction_class": "spot"},
            "generation": 1,
            "updated_at_ms": 1000,
            "external_info": {"connection_id": "spot-1", "last_heartbeat_ms": 1000},
        },
        "std-1": {
            "pid": "std-1",
            "state": "busy",
            "kind": "unmanaged",
            "labels": {"eviction_class": "standard"},
            "generation": 1,
            "updated_at_ms": 2000,
            "external_info": {"connection_id": "std-1", "last_heartbeat_ms": 2000},
        },
    }

    class FakeTransport:
        def __init__(self) -> None:
            self.signalled: list[str] = []

        def call_rpc(
            self,
            method: str,
            params: dict[str, object],
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                return list(agents.values())
            if method == "agent_get":
                return agents.get(params["pid"])
            if method == "agent_signal":
                self.signalled.append(str(params["pid"]))
                return agents.get(params["pid"])
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    transport = FakeTransport()
    client._transport = transport

    monitor = AsyncMock()
    monitor.check_pressure.return_value = PressureLevel.CRITICAL

    manager = EvictionManager(
        agent_registry=client.agent_registry,
        monitor=monitor,
        policy=QoSEvictionPolicy(),
        tuning=EvictionTuning(
            memory_high_watermark_pct=85,
            memory_low_watermark_pct=75,
            max_active_agents=100,
            eviction_batch_size=5,
            checkpoint_timeout_seconds=5.0,
            eviction_cooldown_seconds=60,
            max_concurrent_transitions=10,
        ),
    )

    result = asyncio.run(manager.run_cycle())

    assert result.reason is EvictionReason.PRESSURE_CRITICAL
    assert result.evicted == 2
    # spot evicted before standard (QoS ordering) — both signalled.
    assert sorted(transport.signalled) == ["spot-1", "std-1"]


def test_eviction_cycle_over_cap_path_drives_count_by_state_through_proxy() -> None:
    """Reproduce issue #4268's EXACT traceback: count_by_state at eviction_manager.py:157.

    That call lives inside `if pressure is PressureLevel.NORMAL:`, so it only runs on
    a NORMAL-pressure / over-agent-cap cycle. The CRITICAL-pressure regression tests
    skip it entirely. This drives the over-cap branch end-to-end through run_cycle with
    the production-wired QoSEvictionPolicy + real proxy, so the count_by_state crash
    path the issue reported is covered through the manager, not just in isolation.
    """
    import asyncio
    from unittest.mock import AsyncMock

    from nexus.contracts.agent_types import EvictionReason
    from nexus.lib.performance_tuning import EvictionTuning
    from nexus.services.agents.eviction_manager import EvictionManager
    from nexus.services.agents.eviction_policy import QoSEvictionPolicy
    from nexus.services.agents.resource_monitor import PressureLevel

    agents = {
        "spot-1": {
            "pid": "spot-1",
            "state": "busy",
            "kind": "unmanaged",
            "labels": {"eviction_class": "spot"},
            "generation": 1,
            "updated_at_ms": 1000,
            "external_info": {"connection_id": "spot-1", "last_heartbeat_ms": 1000},
        },
        "std-1": {
            "pid": "std-1",
            "state": "busy",
            "kind": "unmanaged",
            "labels": {"eviction_class": "standard"},
            "generation": 1,
            "updated_at_ms": 2000,
            "external_info": {"connection_id": "std-1", "last_heartbeat_ms": 2000},
        },
    }

    class FakeTransport:
        def __init__(self) -> None:
            self.signalled: list[str] = []
            self.methods: list[str] = []

        def call_rpc(
            self,
            method: str,
            params: dict[str, object],
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            self.methods.append(method)
            if method == "agent_list":
                return list(agents.values())
            if method == "agent_get":
                return agents.get(params["pid"])
            if method == "agent_signal":
                self.signalled.append(str(params["pid"]))
                return agents.get(params["pid"])
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    transport = FakeTransport()
    client._transport = transport

    monitor = AsyncMock()
    # NORMAL pressure → the over-cap branch (count_by_state at :157) runs.
    monitor.check_pressure.return_value = PressureLevel.NORMAL

    manager = EvictionManager(
        agent_registry=client.agent_registry,
        monitor=monitor,
        policy=QoSEvictionPolicy(),
        tuning=EvictionTuning(
            memory_high_watermark_pct=85,
            memory_low_watermark_pct=75,
            max_active_agents=1,  # 2 BUSY agents > cap 1 → over_cap triggers
            eviction_batch_size=5,
            checkpoint_timeout_seconds=5.0,
            eviction_cooldown_seconds=60,
            max_concurrent_transitions=10,
        ),
    )

    result = asyncio.run(manager.run_cycle())

    # count_by_state was actually exercised through run_cycle (the #4268 crash line).
    assert "agent_list" in transport.methods
    assert result.reason is EvictionReason.OVER_AGENT_CAP
    assert result.evicted == 2
    assert sorted(transport.signalled) == ["spot-1", "std-1"]


def test_agent_registry_proxy_list_by_priority_handles_missing_updated_at_ms() -> None:
    """Agents with absent or None updated_at_ms sort as 0 (front) and never raise —
    guards the `getattr(a, 'updated_at_ms', 0) or 0` fallback in list_by_priority."""

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                return [
                    {
                        "pid": "has",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {},
                        "updated_at_ms": 500,
                    },
                    {
                        "pid": "none",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {},
                        "updated_at_ms": None,
                    },
                    # 'missing' omits updated_at_ms entirely.
                    {"pid": "missing", "state": "busy", "kind": "unmanaged", "labels": {}},
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    result = client.agent_registry.list_by_priority()

    pids = [a.pid for a in result]
    # None/missing treated as 0 → both sort ahead of has (500); no TypeError raised.
    assert pids[-1] == "has"
    assert set(pids[:2]) == {"none", "missing"}


def test_agent_registry_proxy_list_by_priority_parses_eviction_priority_like_rust() -> None:
    """eviction_priority is parsed with Rust parse::<i64>().unwrap_or(50) semantics.

    Python int() is too lenient (accepts " 5", "1_000", >i64 bignums) and would
    order agents differently than the in-process kernel. Only a plain ASCII signed
    decimal within i64 range counts; everything else defaults to 50.
    """

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                # All share updated_at_ms so ordering is decided purely by priority.
                return [
                    {
                        "pid": "neg",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "-3"},
                        "updated_at_ms": 100,
                    },
                    {
                        "pid": "plus7",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "+7"},
                        "updated_at_ms": 100,
                    },
                    {
                        "pid": "plain10",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "10"},
                        "updated_at_ms": 100,
                    },
                    {
                        "pid": "spacey",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": " 5"},
                        "updated_at_ms": 100,
                    },
                    {
                        "pid": "under",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "1_000"},
                        "updated_at_ms": 100,
                    },
                    {
                        "pid": "bignum",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "9223372036854775808"},
                        "updated_at_ms": 100,
                    },
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    order = [a.pid for a in client.agent_registry.list_by_priority()]

    # Valid decimals sort by value: neg(-3) < plus7(7) < plain10(10).
    assert order[0] == "neg"
    assert order[1] == "plus7"
    assert order[2] == "plain10"
    # " 5", "1_000", and the >i64 bignum all default to 50 (Rust would reject them).
    assert set(order[3:]) == {"spacey", "under", "bignum"}


def test_agent_registry_proxy_list_by_priority_overlong_priority_does_not_crash() -> None:
    """An all-digit eviction_priority longer than i64 must default to 50, never crash.

    CPython caps int(str) at 4300 digits and raises ValueError beyond it; a
    malformed multi-thousand-digit label must not take down the eviction cycle.
    Any 20+ digit value also exceeds i64, so Rust parse::<i64> defaults it to 50.
    """

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                return [
                    {
                        "pid": "overlong",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "9" * 5000},
                        "updated_at_ms": 100,
                    },
                    {
                        "pid": "low",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {"eviction_priority": "1"},
                        "updated_at_ms": 100,
                    },
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    # Must not raise (default 5000-digit -> 50, below 'low' which is priority 1).
    order = [a.pid for a in client.agent_registry.list_by_priority()]
    assert order == ["low", "overlong"]


def test_agent_registry_proxy_list_by_priority_zero_padded_priority_parses_like_rust() -> None:
    """Leading-zero eviction_priority parses by value, matching Rust parse::<i64>().

    A zero-padded label longer than 19 chars (e.g. "0000000000000000000001")
    is still 1 to Rust; it must NOT be rejected as overlong/default-50. The
    proxy normalizes leading zeros before the significant-digit length check.
    """

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_list":
                return [
                    {
                        "pid": "zpad_one",
                        "state": "busy",
                        "kind": "unmanaged",
                        # 21 chars total, significant value 1.
                        "labels": {"eviction_priority": "0" * 20 + "1"},
                        "updated_at_ms": 100,
                    },
                    {
                        "pid": "default50",
                        "state": "busy",
                        "kind": "unmanaged",
                        "labels": {},  # no label -> 50
                        "updated_at_ms": 100,
                    },
                ]
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    order = [a.pid for a in client.agent_registry.list_by_priority()]
    # zpad_one parses to 1 (< default 50), so it is the first eviction candidate.
    assert order == ["zpad_one", "default50"]


def test_agent_registry_proxy_descriptor_to_dict_matches_agentdescriptor_shape() -> None:
    """Proxy descriptors expose a to_dict() compatible with AgentDescriptor.to_dict().

    Regression for issue #4268: the proc/status VFS resolvers serialize agents
    via desc.to_dict(); a bare SimpleNamespace lacked the method and raised
    AttributeError in subprocess-kernel mode.
    """
    from datetime import UTC, datetime

    from nexus.contracts.process_types import AgentDescriptor, AgentKind, AgentState

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],  # noqa: ARG002
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_get":
                return {
                    "pid": "a1",
                    "ppid": None,
                    "name": "agent",
                    "state": "busy",
                    "kind": "unmanaged",
                    "owner_id": "u",
                    "zone_id": "z",
                    "generation": 3,
                    "created_at_ms": 1_700_000_000_000,
                    "updated_at_ms": 1_700_000_005_000,
                    "labels": {"capabilities": "search,cache", "eviction_class": "spot"},
                    "external_info": {
                        "connection_id": "c1",
                        "host_pid": 42,
                        "remote_addr": "10.0.0.1",
                        "protocol": "grpc",
                        "last_heartbeat_ms": 1_700_000_009_000,
                    },
                }
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    desc = client.agent_registry.get("a1")
    d = desc.to_dict()

    # Key set must match the real AgentDescriptor.to_dict() output exactly.
    now = datetime.now(UTC)
    real_keys = set(
        AgentDescriptor(
            pid="a1",
            ppid=None,
            name="agent",
            kind=AgentKind.UNMANAGED,
            state=AgentState.BUSY,
            owner_id="u",
            zone_id="z",
            generation=3,
            created_at=now,
            updated_at=now,
            labels={},
        ).to_dict()
    )
    assert set(d) == real_keys
    assert d["pid"] == "a1"
    assert d["state"] == "busy"
    assert d["kind"] == "unmanaged"
    assert d["generation"] == 3
    assert d["labels"]["eviction_class"] == "spot"
    # capabilities is a proxy-only convenience attribute, NOT a to_dict() key
    # (matching AgentDescriptor.to_dict(), which has no capabilities key).
    assert "capabilities" not in d
    assert desc.capabilities == ["search", "cache"]
    # Timestamps serialized as ISO-8601 strings, not epoch-ms.
    assert isinstance(d["created_at"], str) and "T" in d["created_at"]
    assert isinstance(d["updated_at"], str) and "T" in d["updated_at"]
    assert d["external_info"]["connection_id"] == "c1"
    assert d["external_info"]["host_pid"] == 42
    assert isinstance(d["external_info"]["last_heartbeat"], str)
    # Must be JSON-serializable (the resolver does json.dumps(desc.to_dict())).
    import json as _json

    _json.dumps(d)


def test_agent_status_resolver_reads_proc_status_via_proxy_registry() -> None:
    """AgentStatusResolver.try_read serves /{zone}/proc/{pid}/status against the
    real _AgentRegistryProxy without raising — the exact consumer codex flagged."""
    import json as _json

    from nexus.services.agents.agent_status_resolver import AgentStatusResolver

    class FakeTransport:
        def call_rpc(
            self,
            method: str,
            params: dict[str, object],
            auth_token: str | None = None,  # noqa: ARG002
        ) -> object:
            if method == "agent_get" and params.get("pid") == "pid-1":
                return {
                    "pid": "pid-1",
                    "name": "agent",
                    "state": "busy",
                    "kind": "unmanaged",
                    "owner_id": "u",
                    "zone_id": "z",
                    "generation": 1,
                    "created_at_ms": 1_700_000_000_000,
                    "updated_at_ms": 1_700_000_001_000,
                    "labels": {},
                    "external_info": {
                        "connection_id": "pid-1",
                        "last_heartbeat_ms": 1_700_000_001_000,
                    },
                }
            return None

    client = KernelClient(server_address="127.0.0.1:1")
    client._transport = FakeTransport()

    resolver = AgentStatusResolver(client.agent_registry)
    out = resolver.try_read("/z/proc/pid-1/status")

    assert out is not None
    parsed = _json.loads(out.decode())
    assert parsed["pid"] == "pid-1"
    assert parsed["state"] == "busy"
    # Unknown pid → None (not a crash).
    assert resolver.try_read("/z/proc/missing/status") is None


def test_metastore_list_paginated_preserves_directory_entries() -> None:
    class FakeClient(KernelClient):
        entry_types = {
            "/alpha": DT_DIR,
            "/alpha/child.txt": DT_REG,
            "/empty": DT_DIR,
            "/file.txt": DT_REG,
        }

        def __init__(self) -> None:
            pass

        def sys_readdir(
            self,
            path: str,
            zone_id: str = "root",  # noqa: ARG002
            is_admin: bool = False,  # noqa: ARG002
        ) -> list[tuple[str, int]]:
            return {
                "/": [("/alpha", DT_DIR), ("/empty", DT_DIR), ("/file.txt", DT_REG)],
                "/alpha": [("/alpha/child.txt", DT_REG)],
                "/empty": [],
            }.get(path, [])

        def stat_batch(self, paths: list[str]) -> list[dict[str, object]]:
            return [
                {
                    "path": path,
                    "size": 0,
                    "entry_type": self.entry_types[path],
                    "zone_id": "root",
                }
                for path in paths
            ]

    client = FakeClient()

    direct = client.metastore_list_paginated("/", recursive=False, limit=100, cursor=None)
    recursive = client.metastore_list_paginated("/", recursive=True, limit=100, cursor=None)

    assert [(item.path, item.entry_type) for item in direct["items"]] == [
        ("/alpha", DT_DIR),
        ("/empty", DT_DIR),
        ("/file.txt", DT_REG),
    ]
    assert [(item.path, item.entry_type) for item in recursive["items"]] == [
        ("/alpha", DT_DIR),
        ("/alpha/child.txt", DT_REG),
        ("/empty", DT_DIR),
        ("/file.txt", DT_REG),
    ]

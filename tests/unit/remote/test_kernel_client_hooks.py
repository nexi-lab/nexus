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

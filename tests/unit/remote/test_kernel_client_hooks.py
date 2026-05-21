from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from nexus.remote.kernel_client import (
    KernelClient,
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


def test_kernel_client_sys_read_honors_nonblocking_timeout() -> None:
    class FakeTransport:
        def __init__(self) -> None:
            self.read_file_calls = 0
            self.call_rpc_calls: list[tuple[str, dict[str, object]]] = []

        def read_file(self, *_args: object, **_kwargs: object) -> bytes:
            self.read_file_calls += 1
            return b"typed"

        def call_rpc(
            self,
            method: str,
            params: dict[str, object],
            auth_token: str | None = None,  # noqa: ARG002
        ) -> dict[str, object]:
            self.call_rpc_calls.append((method, params))
            return {
                "data": b"",
                "content_id": None,
                "gen": 0,
                "entry_type": 3,
                "stream_next_offset": None,
                "post_hook_needed": False,
            }

    client = KernelClient(server_address="127.0.0.1:1")
    transport = FakeTransport()
    client._transport = transport

    result = client.sys_read("/nexus/pipes/task-dispatch", timeout_ms=0)

    assert result.data == b""
    assert result.entry_type == 3
    assert transport.read_file_calls == 0
    assert transport.call_rpc_calls == [
        ("sys_read", {"path": "/nexus/pipes/task-dispatch", "timeout_ms": 0, "offset": 0})
    ]

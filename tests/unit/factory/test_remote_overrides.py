"""Tests for REMOTE profile NexusFS method overrides."""

from types import SimpleNamespace
from typing import Any, cast

from nexus.factory._remote import install_remote_kernel_rpc_overrides


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.typed_reads: list[str] = []
        self.typed_writes: list[tuple[str, bytes]] = []

    def call_rpc(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "sys_read":
            return b"rpc-read"
        if method in {"write", "sys_write"}:
            return {"path": params["path"], "bytes_written": len(params["buf"])}
        if method == "sys_rename":
            return {"renamed": True}
        raise AssertionError(f"unexpected RPC method: {method}")

    def read_file(self, path: str) -> bytes:
        self.typed_reads.append(path)
        return b"typed-read"

    def write_file(self, path: str, content: bytes) -> dict[str, Any]:
        self.typed_writes.append((path, content))
        return {"etag": "typed", "size": len(content)}


def test_remote_sys_read_uses_generic_rpc_for_server_side_scoping() -> None:
    transport = _FakeTransport()
    nfs = SimpleNamespace()

    install_remote_kernel_rpc_overrides(cast(Any, nfs), cast(Any, transport))

    assert nfs.sys_read("/workspace/file.txt") == b"rpc-read"
    assert nfs.sys_read("/workspace/file.txt", count=4, offset=2) == b"rpc-read"

    assert transport.typed_reads == []
    assert transport.calls == [
        ("sys_read", {"path": "/workspace/file.txt"}),
        ("sys_read", {"path": "/workspace/file.txt", "count": 4, "offset": 2}),
    ]


def test_remote_write_methods_use_generic_rpc_for_server_side_scoping() -> None:
    transport = _FakeTransport()
    nfs = SimpleNamespace()

    install_remote_kernel_rpc_overrides(cast(Any, nfs), cast(Any, transport))

    assert nfs.write("/workspace/file.txt", "abcdef", count=3, offset=5) == {
        "path": "/workspace/file.txt",
        "bytes_written": 3,
    }
    assert nfs.sys_write("/workspace/raw.txt", b"abcdef", count=2) == {
        "path": "/workspace/raw.txt",
        "bytes_written": 2,
    }

    assert transport.typed_writes == []
    assert transport.calls == [
        ("write", {"path": "/workspace/file.txt", "buf": b"abc", "offset": 5}),
        ("sys_write", {"path": "/workspace/raw.txt", "buf": b"ab"}),
    ]


def test_remote_sys_rename_uses_generic_rpc_for_post_rename_hooks() -> None:
    transport = _FakeTransport()
    nfs = SimpleNamespace()

    install_remote_kernel_rpc_overrides(cast(Any, nfs), cast(Any, transport))

    assert nfs.sys_rename("/workspace/old.txt", "/workspace/new.txt", force=True) == {}
    assert transport.calls == [
        (
            "sys_rename",
            {
                "old_path": "/workspace/old.txt",
                "new_path": "/workspace/new.txt",
                "force": True,
            },
        )
    ]

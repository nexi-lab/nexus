"""Capability gating tests for REMOTE NexusFS method overrides."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.contracts.exceptions import RemoteCapabilityUnsupportedError
from nexus.factory._remote import install_remote_kernel_rpc_overrides
from nexus.grpc.capability_discovery import writable_posix


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_rpc(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method in {"write", "sys_write", "sys_unlink", "mkdir", "rmdir"}:
            return {"ok": True}
        if method == "sys_rename":
            return {"renamed": True}
        raise AssertionError(f"unexpected RPC method: {method}")


def _capabilities(**posix_overrides: bool) -> dict[str, Any]:
    posix = writable_posix()
    posix.update(posix_overrides)
    return {"posix": posix}


def test_remote_write_denied_by_capability_raises_before_rpc() -> None:
    transport = _FakeTransport()
    nfs = SimpleNamespace(capabilities=_capabilities(write=False))

    install_remote_kernel_rpc_overrides(cast(Any, nfs), cast(Any, transport))

    with pytest.raises(
        RemoteCapabilityUnsupportedError, match="Remote mount does not declare write"
    ) as exc:
        nfs.write("/workspace/file.txt", b"content")

    assert exc.value.status_code == 501
    assert exc.value.is_expected is True
    assert transport.calls == []


def test_missing_capabilities_preserves_legacy_remote_write() -> None:
    transport = _FakeTransport()
    nfs = SimpleNamespace(capabilities=None)

    install_remote_kernel_rpc_overrides(cast(Any, nfs), cast(Any, transport))

    assert nfs.write("/workspace/file.txt", b"content") == {"ok": True}
    assert transport.calls == [
        ("write", {"path": "/workspace/file.txt", "buf": b"content"}),
    ]


@pytest.mark.parametrize(
    ("method_name", "path", "capability", "args", "kwargs"),
    [
        ("sys_unlink", "/workspace/file.txt", "unlink", (), {}),
        ("mkdir", "/workspace/dir", "mkdir", (), {}),
        ("rmdir", "/workspace/dir", "rmdir", (), {}),
    ],
)
def test_remote_mutations_denied_by_capability_raise_before_rpc(
    method_name: str,
    path: str,
    capability: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    transport = _FakeTransport()
    nfs = SimpleNamespace(capabilities=_capabilities(**{capability: False}))

    install_remote_kernel_rpc_overrides(cast(Any, nfs), cast(Any, transport))

    with pytest.raises(
        RemoteCapabilityUnsupportedError, match=f"Remote mount does not declare {capability}"
    ):
        getattr(nfs, method_name)(path, *args, **kwargs)

    assert transport.calls == []


def test_remote_rename_checks_destination_capability_before_rpc() -> None:
    transport = _FakeTransport()
    posix = writable_posix()
    readonly = writable_posix()
    readonly["rename"] = False
    nfs = SimpleNamespace(
        capabilities={
            "posix": posix,
            "backends": {
                "/": {"posix": posix},
                "/readonly": {"posix": readonly},
            },
        }
    )

    install_remote_kernel_rpc_overrides(cast(Any, nfs), cast(Any, transport))

    with pytest.raises(
        RemoteCapabilityUnsupportedError, match="Remote mount does not declare rename"
    ):
        nfs.sys_rename("/workspace/old.txt", "/readonly/new.txt")

    assert transport.calls == []

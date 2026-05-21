from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus.grpc.vfs import vfs_pb2
from nexus.lib.rpc_codec import encode_rpc_message
from nexus.server.lifespan import vfs_grpc


class _FakeGrpcContext:
    def __init__(self, peer: str = "ipv4:203.0.113.10:44444") -> None:
        self._peer = peer

    def peer(self) -> str:
        return self._peer


@pytest.mark.asyncio
async def test_call_auth_uses_real_grpc_peer_host(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _resolve_auth(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "alice",
            "zone_id": "default",
        }

    monkeypatch.setattr(vfs_grpc, "resolve_auth", _resolve_auth)
    app = SimpleNamespace(state=SimpleNamespace(exposed_methods={}))
    servicer = vfs_grpc.VFSGrpcServicer(app)

    await servicer.Call(
        vfs_pb2.CallRequest(
            method="unknown_method",
            payload=encode_rpc_message({}),
            auth_token="sk-test",
        ),
        _FakeGrpcContext(),
    )

    assert captured["client_host"] == "203.0.113.10"


@pytest.mark.asyncio
async def test_write_forwards_content_id_as_if_match(monkeypatch: pytest.MonkeyPatch) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    servicer = vfs_grpc.VFSGrpcServicer(app)
    captured: dict[str, Any] = {}

    async def _dispatch(
        method: str,
        params: dict[str, Any],
        token: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        captured.update({"method": method, "params": params, "token": token})
        return {"content_id": "new-content", "size": 3, "gen": 2}

    monkeypatch.setattr(servicer, "_dispatch", _dispatch)

    response = await servicer.Write(
        vfs_pb2.WriteRequest(
            path="/workspace/file.txt",
            content=b"new",
            auth_token="sk-test",
            content_id="old-content",
        ),
        _FakeGrpcContext(),
    )

    assert response.is_error is False
    assert captured == {
        "method": "sys_write",
        "params": {
            "path": "/workspace/file.txt",
            "buf": b"new",
            "if_match": "old-content",
        },
        "token": "sk-test",
    }

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.contracts.exceptions import NexusPermissionError
from nexus.contracts.rpc_types import RPCErrorCode
from nexus.grpc.servicer import VFSCallDispatcher
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message


@pytest.mark.asyncio
async def test_dispatch_async_maps_permission_errors(monkeypatch):
    async def deny(*_args, **_kwargs):
        raise NexusPermissionError("Admin privileges required for this operation")

    monkeypatch.setattr("nexus.server.rpc.dispatch.dispatch_method", deny)
    dispatcher = VFSCallDispatcher(
        nexus_fs=MagicMock(),
        exposed_methods={},
        loop=MagicMock(),
        auth_provider=MagicMock(),
    )

    is_error, payload = await dispatcher._dispatch_async(
        "hub_admin_token_list",
        encode_rpc_message({"show_revoked": False}),
        {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "bob",
            "is_admin": False,
        },
    )

    assert is_error is True
    error = decode_rpc_message(payload)
    assert error["code"] == RPCErrorCode.PERMISSION_ERROR.value
    assert "Admin privileges required for this operation" in error["message"]

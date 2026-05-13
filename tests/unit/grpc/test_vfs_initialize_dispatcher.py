import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from nexus.grpc.capability_discovery import PROTOCOL_VERSION
from nexus.grpc.servicer import VFSCallDispatcher


@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def test_initialize_sync_returns_capability_payload(event_loop) -> None:
    kernel = SimpleNamespace(get_mount_points=lambda: ["/root"])
    nexus_fs = SimpleNamespace(_kernel=kernel)
    dispatcher = VFSCallDispatcher(
        nexus_fs=nexus_fs,
        exposed_methods={"grep": object(), "glob": object(), "workspace_snapshot": object()},
        api_key=None,
        loop=event_loop,
    )

    payload = dispatcher.initialize_sync(
        {
            "client_name": "pytest",
            "client_version": "0.0",
            "protocol_version": PROTOCOL_VERSION,
        },
        {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "alice",
            "zone_id": "root",
            "is_admin": False,
        },
        {},
    )

    assert payload["server_name"] == "nexus"
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["capabilities"]["commands"]["grep"]["supported"] is True
    assert "/" in payload["capabilities"]["backends"]

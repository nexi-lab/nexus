from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from nexus.bricks.auth.daemon.push import Pusher, PushError
from nexus.bricks.auth.daemon.queue import PushQueue


@dataclass
class FakeEnvelope:
    ciphertext: bytes
    wrapped_dek: bytes
    nonce: bytes
    aad: bytes
    kek_version: int


class FakeProvider:
    def encrypt(
        self,
        plaintext: bytes,
        *,
        tenant_id: uuid.UUID,  # noqa: ARG002 - Protocol parity with real EncryptionProvider
        aad: bytes,
    ) -> FakeEnvelope:
        return FakeEnvelope(
            ciphertext=b"ctx-" + plaintext[:8],
            wrapped_dek=b"dek",
            nonce=b"\x00" * 12,
            aad=aad,
            kek_version=1,
        )


def _make_pusher(tmp_path: Path) -> tuple[Pusher, PushQueue, MagicMock]:
    queue = PushQueue(tmp_path / "queue.db")
    jwt_provider = MagicMock(return_value="fake-jwt")
    pusher = Pusher(
        server_url="https://test.nexus",
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
        daemon_version="0.9.20",
        encryption_provider=FakeProvider(),
        queue=queue,
        jwt_provider=jwt_provider,
    )
    return pusher, queue, jwt_provider


@respx.mock
def test_push_happy_path_clears_queue(tmp_path: Path) -> None:
    pusher, queue, _jp = _make_pusher(tmp_path)
    respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    pusher.push_source("codex", content=b'{"token":"abc"}', provider="codex")
    assert queue.list_pending() == []


@respx.mock
def test_hash_dedupe_skips_second_push(tmp_path: Path) -> None:
    pusher, queue, _jp = _make_pusher(tmp_path)
    route = respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    pusher.push_source("codex", content=b'{"token":"abc"}', provider="codex")
    pusher.push_source("codex", content=b'{"token":"abc"}', provider="codex")
    assert route.call_count == 1


@respx.mock
def test_push_network_fail_leaves_queue_dirty(tmp_path: Path) -> None:
    pusher, queue, _jp = _make_pusher(tmp_path)
    respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(503, text="temporary")
    )
    with pytest.raises(PushError):
        pusher.push_source("codex", content=b'{"token":"abc"}', provider="codex")
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].attempts >= 1


@respx.mock
def test_push_401_raises_auth_stale(tmp_path: Path) -> None:
    pusher, queue, _jp = _make_pusher(tmp_path)
    respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    with pytest.raises(PushError, match="auth_stale"):
        pusher.push_source("codex", content=b'{"token":"abc"}', provider="codex")

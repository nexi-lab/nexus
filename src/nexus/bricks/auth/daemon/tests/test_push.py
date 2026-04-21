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


def _make_pusher(
    tmp_path: Path,
    *,
    refresh_jwt: MagicMock | None = None,
) -> tuple[Pusher, PushQueue, MagicMock]:
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
        refresh_jwt=refresh_jwt,
    )
    return pusher, queue, jwt_provider


@respx.mock
def test_push_happy_path_clears_queue(tmp_path: Path) -> None:
    pusher, queue, _jp = _make_pusher(tmp_path)
    respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    pusher.push_source(
        "codex", content=b'{"token":"abc"}', provider="codex", account_identifier="u@example.com"
    )
    assert queue.list_pending() == []


@respx.mock
def test_hash_dedupe_skips_second_push(tmp_path: Path) -> None:
    pusher, queue, _jp = _make_pusher(tmp_path)
    route = respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    pusher.push_source(
        "codex", content=b'{"token":"abc"}', provider="codex", account_identifier="u@example.com"
    )
    pusher.push_source(
        "codex", content=b'{"token":"abc"}', provider="codex", account_identifier="u@example.com"
    )
    assert route.call_count == 1


@respx.mock
def test_push_network_fail_leaves_queue_dirty(tmp_path: Path) -> None:
    pusher, queue, _jp = _make_pusher(tmp_path)
    respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(503, text="temporary")
    )
    with pytest.raises(PushError):
        pusher.push_source(
            "codex",
            content=b'{"token":"abc"}',
            provider="codex",
            account_identifier="u@example.com",
        )
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
        pusher.push_source(
            "codex",
            content=b'{"token":"abc"}',
            provider="codex",
            account_identifier="u@example.com",
        )


@respx.mock
def test_push_401_refresh_and_retry_succeeds(tmp_path: Path) -> None:
    """When ``refresh_jwt`` is wired, a 401 triggers a refresh + one retry."""
    refresh_jwt = MagicMock(return_value="fresh-jwt")
    pusher, queue, jwt_provider = _make_pusher(tmp_path, refresh_jwt=refresh_jwt)
    route = respx.post("https://test.nexus/v1/auth-profiles").mock(
        side_effect=[
            httpx.Response(401, text="auth_stale"),
            httpx.Response(200, json={"status": "ok"}),
        ]
    )
    pusher.push_source(
        "codex", content=b'{"token":"abc"}', provider="codex", account_identifier="u@example.com"
    )
    # Both the initial stale JWT AND the fresh JWT must have been sent.
    assert route.call_count == 2
    assert refresh_jwt.call_count == 1
    assert queue.list_pending() == []


@respx.mock
def test_push_401_refresh_then_still_401_records_stale(tmp_path: Path) -> None:
    """If the forced refresh still yields 401, the original auth_stale fires."""
    refresh_jwt = MagicMock(return_value="fresh-but-also-bad")
    pusher, queue, _jp = _make_pusher(tmp_path, refresh_jwt=refresh_jwt)
    respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(401, text="auth_stale"),
    )
    with pytest.raises(PushError, match="auth_stale"):
        pusher.push_source(
            "codex",
            content=b'{"token":"abc"}',
            provider="codex",
            account_identifier="u@example.com",
        )
    assert refresh_jwt.call_count == 1


@respx.mock
def test_push_401_refresh_raises_falls_through(tmp_path: Path) -> None:
    """A raised refresh_jwt is swallowed; original 401 becomes auth_stale."""
    refresh_jwt = MagicMock(side_effect=RuntimeError("refresh broke"))
    pusher, queue, _jp = _make_pusher(tmp_path, refresh_jwt=refresh_jwt)
    respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(401, text="auth_stale"),
    )
    with pytest.raises(PushError, match="auth_stale"):
        pusher.push_source(
            "codex",
            content=b'{"token":"abc"}',
            provider="codex",
            account_identifier="u@example.com",
        )
    assert refresh_jwt.call_count == 1


def test_push_rejects_empty_account_identifier(tmp_path: Path) -> None:
    """Refuse to push when caller can't name the account — no more 'unknown'."""
    pusher, _q, _jp = _make_pusher(tmp_path)
    with pytest.raises(PushError, match="account_identifier required"):
        pusher.push_source(
            "codex", content=b'{"token":"abc"}', provider="codex", account_identifier=""
        )


def test_push_rejects_unknown_sentinel(tmp_path: Path) -> None:
    """Explicit 'unknown' is also rejected so old callers fail loudly."""
    pusher, _q, _jp = _make_pusher(tmp_path)
    with pytest.raises(PushError, match="account_identifier required"):
        pusher.push_source(
            "codex", content=b'{"token":"abc"}', provider="codex", account_identifier="unknown"
        )


@respx.mock
def test_push_retry_preserves_client_updated_at(tmp_path: Path) -> None:
    """Retries of the same payload must reuse the original client_updated_at.

    Regression: subprocess sources have no natural source_mtime. Before the
    fix, each retry regenerated now() so a stale retry after network recovery
    claimed to be freshly written and bypassed server-side stale-ordering.
    With the queue-backed fallback, the first enqueue stamps a timestamp and
    every retry of the same (profile_id, payload_hash) reuses it.
    """
    import json as _json

    pusher, _q, _jp = _make_pusher(tmp_path)
    # First response fails (503 transient), second succeeds.
    route = respx.post("https://test.nexus/v1/auth-profiles").mock(
        side_effect=[
            httpx.Response(503, text="down"),
            httpx.Response(200, json={"status": "ok"}),
        ]
    )
    content = b'{"token":"same"}'
    # First attempt fails → stays pending, queue captures enqueued_at.
    with pytest.raises(PushError):
        pusher.push_source(
            "codex", content=content, provider="codex", account_identifier="u@example.com"
        )
    first_body = _json.loads(route.calls[0].request.content)
    first_ts = first_body["client_updated_at"]

    # Second attempt (same content) must send the SAME client_updated_at.
    pusher.push_source(
        "codex", content=content, provider="codex", account_identifier="u@example.com"
    )
    second_body = _json.loads(route.calls[1].request.content)
    assert second_body["client_updated_at"] == first_ts


@respx.mock
def test_push_payload_includes_client_updated_at(tmp_path: Path) -> None:
    """Daemon always sends ``client_updated_at`` so server conflict detection fires.

    Regression: previously the server's conflict-detection field was test-only
    and was never populated in production pushes, disabling the warning for
    real cross-daemon races. The daemon must populate it on every push.
    """
    from datetime import UTC, datetime

    pusher, _q, _jp = _make_pusher(tmp_path)
    route = respx.post("https://test.nexus/v1/auth-profiles").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    # With explicit source_mtime.
    fixed = datetime(2026, 3, 5, 12, 0, 0, tzinfo=UTC)
    pusher.push_source(
        "codex",
        content=b'{"token":"abc"}',
        provider="codex",
        account_identifier="u@example.com",
        source_mtime=fixed,
    )
    import json as _json

    first_body = _json.loads(route.calls.last.request.content)
    assert first_body["client_updated_at"] == fixed.isoformat()

    # Without source_mtime, daemon substitutes wall-clock NOW.
    pusher.push_source(
        "codex",
        content=b'{"token":"def"}',
        provider="codex",
        account_identifier="u@example.com",
    )
    second_body = _json.loads(route.calls.last.request.content)
    assert "client_updated_at" in second_body
    assert second_body["client_updated_at"] != fixed.isoformat()

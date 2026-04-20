"""Daemon push logic: dedupe, envelope, POST, queue bookkeeping (#3804)."""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from nexus.bricks.auth.daemon.queue import PushQueue

log = logging.getLogger(__name__)


class PushError(Exception):
    """Push failed. Message body is tagged with an error class string."""


class _Envelope(Protocol):
    ciphertext: bytes
    wrapped_dek: bytes
    nonce: bytes
    aad: bytes
    kek_version: int


class _EncryptionProvider(Protocol):
    def encrypt(self, plaintext: bytes, *, tenant_id: uuid.UUID, aad: bytes) -> _Envelope: ...


@dataclass
class _LastPushed:
    """In-memory map of last-pushed hash per source. Survives while daemon runs."""

    hashes: dict[str, str]

    def __init__(self) -> None:
        self.hashes = {}


class Pusher:
    def __init__(
        self,
        *,
        server_url: str,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        machine_id: uuid.UUID,
        daemon_version: str,
        encryption_provider: _EncryptionProvider,
        queue: PushQueue,
        jwt_provider: Callable[[], str],
        http: httpx.Client | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._tenant_id = tenant_id
        self._principal_id = principal_id
        self._machine_id = machine_id
        self._daemon_version = daemon_version
        self._ep = encryption_provider
        self._queue = queue
        self._jwt_provider = jwt_provider
        self._http = http or httpx.Client(timeout=10.0)
        self._last_pushed = _LastPushed()

    @staticmethod
    def _source_to_provider(source: str) -> str:
        return {
            "codex": "codex",
            "gcloud": "google",
            "gh": "github",
            "gws": "google-workspace",
        }.get(source, source)

    def push_source(
        self,
        source: str,
        *,
        content: bytes,
        provider: str | None = None,
        account_identifier: str = "unknown",
    ) -> None:
        """Push one source's raw content; skip if hash unchanged."""
        new_hash = hashlib.sha256(content).hexdigest()
        provider_name = provider or self._source_to_provider(source)
        profile_id = f"{provider_name}/{account_identifier}"

        if self._last_pushed.hashes.get(profile_id) == new_hash:
            log.debug("push skipped: hash unchanged id=%s", profile_id)
            return

        envelope = self._ep.encrypt(
            content,
            tenant_id=self._tenant_id,
            aad=f"{self._tenant_id}|{self._principal_id}|{profile_id}".encode(),
        )
        self._queue.enqueue(profile_id, payload_hash=new_hash)

        payload = {
            "id": profile_id,
            "provider": provider_name,
            "account_identifier": account_identifier,
            "backend": "nexus-daemon",
            "backend_key": source,
            "envelope": {
                "ciphertext_b64": base64.b64encode(envelope.ciphertext).decode(),
                "wrapped_dek_b64": base64.b64encode(envelope.wrapped_dek).decode(),
                "nonce_b64": base64.b64encode(envelope.nonce).decode(),
                "aad_b64": base64.b64encode(envelope.aad).decode(),
                "kek_version": envelope.kek_version,
            },
            "source_file_hash": new_hash,
        }

        jwt_str = self._jwt_provider()
        try:
            resp = self._http.post(
                f"{self._server_url}/v1/auth-profiles",
                json=payload,
                headers={"Authorization": f"Bearer {jwt_str}"},
            )
        except httpx.HTTPError as exc:
            self._queue.record_attempt(profile_id, error=f"network:{exc}")
            raise PushError(f"network: {exc}") from exc

        if resp.status_code == 401:
            self._queue.record_attempt(profile_id, error="auth_stale")
            raise PushError("auth_stale")
        if 500 <= resp.status_code < 600:
            self._queue.record_attempt(profile_id, error=f"http_{resp.status_code}")
            raise PushError(f"transient http {resp.status_code}")
        if resp.status_code >= 400:
            self._queue.record_attempt(profile_id, error=f"permanent_{resp.status_code}")
            raise PushError(f"permanent http {resp.status_code}: {resp.text}")

        self._queue.mark_success(profile_id, payload_hash=new_hash)
        self._last_pushed.hashes[profile_id] = new_hash

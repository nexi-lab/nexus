"""Daemon push logic: dedupe, envelope, POST, queue bookkeeping (#3804)."""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
        refresh_jwt: Callable[[], str] | None = None,
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
        # Reactive 401 handling: when the server says auth_stale we ask the
        # caller for a forcibly-refreshed token and retry ONCE. Without this,
        # a single expired cached JWT causes every push to fail until the
        # periodic refresh loop runs (up to ~45min), stalling sync.
        self._refresh_jwt = refresh_jwt
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
        account_identifier: str,
        source_mtime: datetime | None = None,
    ) -> None:
        """Push one source's raw content; skip if hash unchanged.

        ``account_identifier`` is now REQUIRED (no more ``"unknown"`` default).
        Multi-account setups send multiple credentials for the same provider,
        and defaulting all of them to the same identifier caused silent
        overwrite of earlier accounts by later ones. Callers that cannot
        determine the identifier must skip the push rather than emit a
        wildcard — see ``DaemonRunner._on_change`` for the parse-and-skip
        pattern used for the codex source.
        """
        if not account_identifier or account_identifier == "unknown":
            raise PushError(
                "account_identifier required (refuse to collapse accounts to 'unknown')"
            )
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

        # Always populate client_updated_at. The server uses it for advisory
        # cross-daemon conflict detection — if we omit it, a stale daemon can
        # silently clobber a fresher write from another daemon. Prefer the
        # source file mtime when the caller knows it, else wall-clock now().
        client_updated_at = (source_mtime or datetime.now(UTC)).isoformat()
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
            "daemon_version": self._daemon_version,
            "client_updated_at": client_updated_at,
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

        # Reactive refresh-and-retry on 401: the cached JWT was accepted
        # locally but rejected by the server (expired, revoked, rotated
        # signing key). Force a refresh and retry once; if it still fails,
        # fall through to the normal auth_stale record path.
        if resp.status_code == 401 and self._refresh_jwt is not None:
            try:
                fresh_jwt = self._refresh_jwt()
            except Exception as exc:  # noqa: BLE001
                log.warning("jwt force-refresh after 401 failed: %s", exc)
                fresh_jwt = None
            if fresh_jwt:
                try:
                    resp = self._http.post(
                        f"{self._server_url}/v1/auth-profiles",
                        json=payload,
                        headers={"Authorization": f"Bearer {fresh_jwt}"},
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

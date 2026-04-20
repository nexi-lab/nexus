"""Daemon JWT client: signed refresh loop + atomic JWT cache (#3804).

The daemon holds a short-lived ES256 JWT issued by the Nexus server. To renew
it, the daemon POSTs a tiny Ed25519-signed body to ``/v1/daemon/refresh`` and
receives a fresh JWT in return. The wire contract is ``sign-raw`` (see
``nexus.server.api.v1.routers.daemon``): the client sends the
pre-canonicalized JSON string alongside the signature, and the server verifies
the signature over the exact bytes the client sent — eliminating
serializer-drift mismatches.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from nexus.bricks.auth.daemon.keystore import load_private_key, sign_body


class JwtClientError(Exception):
    """Refresh failed (HTTP non-200, network error, malformed response)."""


class JwtClient:
    """Holds the daemon's cached JWT and refreshes it via signed requests.

    The cache is a flat file at ``jwt_cache_path`` (mode 0600). ``current()``
    returns the in-memory copy; ``refresh_now()`` signs a fresh request,
    POSTs it to the server, persists the new JWT, and returns it.
    """

    def __init__(
        self,
        *,
        server_url: str,
        tenant_id: uuid.UUID,
        machine_id: uuid.UUID,
        key_path: Path,
        jwt_cache_path: Path,
        server_pubkey_path: Path,
        http: httpx.Client | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.tenant_id = tenant_id
        self.machine_id = machine_id
        self.key_path = key_path
        self.jwt_cache_path = jwt_cache_path
        self.server_pubkey_path = server_pubkey_path
        self._http = http if http is not None else httpx.Client()
        self._cached: str | None = self._load_cached()

    def _load_cached(self) -> str | None:
        if not self.jwt_cache_path.exists():
            return None
        text = self.jwt_cache_path.read_text().strip()
        return text or None

    def current(self) -> str | None:
        """In-memory cached JWT (or ``None`` if never stored)."""
        return self._cached

    def store_token(self, token: str) -> None:
        """Atomically write the JWT to disk with mode 0600."""
        self.jwt_cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(self.jwt_cache_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        # belt-and-suspenders: umask may override the open() mode
        os.chmod(self.jwt_cache_path, 0o600)
        self._cached = token

    def refresh_now(self) -> str:
        """Sign a fresh request, POST to ``/v1/daemon/refresh``, cache + return."""
        priv = load_private_key(self.key_path)
        now_iso = datetime.now(UTC).isoformat()
        body_raw = json.dumps(
            {
                "machine_id": str(self.machine_id),
                "tenant_id": str(self.tenant_id),
                "timestamp_utc": now_iso,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        sig = sign_body(priv, body_raw.encode("utf-8"))
        payload = {"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()}

        url = f"{self.server_url}/v1/daemon/refresh"
        try:
            resp = self._http.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise JwtClientError(f"refresh network error: {exc}") from exc

        if resp.status_code != 200:
            # Surface the server detail verbatim; tests grep for "revoked" etc.
            try:
                text = resp.text
            except Exception:
                text = "<unreadable>"
            raise JwtClientError(f"refresh failed status={resp.status_code} body={text}")

        try:
            data = resp.json()
            jwt_str = data["jwt"]
        except (KeyError, ValueError) as exc:
            raise JwtClientError(f"refresh response malformed: {exc}") from exc
        if not isinstance(jwt_str, str):
            raise JwtClientError("refresh response 'jwt' is not a string")

        self.store_token(jwt_str)
        return jwt_str

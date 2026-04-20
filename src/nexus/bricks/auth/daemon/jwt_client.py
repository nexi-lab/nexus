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
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from nexus.bricks.auth.daemon.jwt_cache import JwtCache, make_jwt_cache
from nexus.bricks.auth.daemon.keystore import load_private_key, sign_body


class JwtClientError(Exception):
    """Refresh failed (HTTP non-200, network error, malformed response)."""


def _jwt_exp_seconds(token: str) -> float | None:
    """Return the ``exp`` claim (unix seconds) from ``token`` or ``None``.

    Used for expiry-aware refresh scheduling. Signature is NOT verified —
    the daemon trusts its own cached token because it was verified when
    the server issued it; we only need the ``exp`` value to decide when
    to refresh locally. A malformed token returns ``None`` so callers fall
    back to the fixed refresh cadence.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        pad = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad).decode())
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except Exception:
        return None
    return None


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
        cache: JwtCache | None = None,
        keyring_service: str | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.tenant_id = tenant_id
        self.machine_id = machine_id
        self.key_path = key_path
        self.jwt_cache_path = jwt_cache_path
        self.server_pubkey_path = server_pubkey_path
        self._http = http if http is not None else httpx.Client()
        # Pluggable cache: keychain first, fall back to a 0600 file. The
        # keyring service name is profile-scoped so two daemons on the same
        # laptop can't see each other's cached JWT (#3788 Blocker 1).
        if cache is not None:
            self._cache: JwtCache = cache
        elif keyring_service is not None:
            self._cache = make_jwt_cache(jwt_cache_path, service=keyring_service)
        else:
            self._cache = make_jwt_cache(jwt_cache_path)
        self._cached: str | None = self._cache.load()

    def current(self) -> str | None:
        """In-memory cached JWT (or ``None`` if never stored)."""
        return self._cached

    def current_valid(self, margin_s: int = 60) -> str | None:
        """Return the cached JWT iff it has > ``margin_s`` seconds until exp.

        Callers should prefer this over :meth:`current` when about to send
        the token on the wire: a token whose ``exp`` is inside the margin
        will almost certainly be rejected by the server, so we force a
        refresh at the call site instead of waiting for the periodic loop.

        Returns ``None`` when no token is cached, when it's already within
        the margin, or when ``exp`` is undecodable (fail closed).
        """
        token = self._cached
        if token is None:
            return None
        exp = _jwt_exp_seconds(token)
        if exp is None:
            return None
        if exp - time.time() <= margin_s:
            return None
        return token

    def seconds_until_expiry(self, now_s: float | None = None) -> float | None:
        """Seconds until cached token's ``exp`` (or ``None`` if undecodable).

        Negative values indicate the token is already expired.
        """
        token = self._cached
        if token is None:
            return None
        exp = _jwt_exp_seconds(token)
        if exp is None:
            return None
        return exp - (now_s if now_s is not None else time.time())

    def store_token(self, token: str) -> None:
        """Persist the JWT via the configured cache backend (keychain or file)."""
        self._cache.store(token)
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

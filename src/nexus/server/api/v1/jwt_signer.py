"""ES256 JWT signer/verifier for daemon tokens (#3804).

Daemon tokens carry (tenant_id, principal_id, machine_id) and are issued
by the server after successful enrollment or refresh. Verification happens
on every /v1 request authenticated as a daemon.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    EllipticCurvePublicKey,
)

_AUDIENCE = "nexus-daemon"
_ALGORITHM = "ES256"


class JwtVerifyError(Exception):
    """Raised when a JWT cannot be verified (signature, expiry, issuer, audience)."""


@dataclass(frozen=True)
class DaemonClaims:
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    machine_id: uuid.UUID


class JwtSigner:
    """Load an ES256 private key from PEM, sign/verify daemon claims."""

    def __init__(
        self,
        *,
        private_key: EllipticCurvePrivateKey,
        public_key: EllipticCurvePublicKey,
        issuer: str,
    ) -> None:
        self._private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._public_key = public_key
        self._issuer = issuer

    @classmethod
    def from_pem(cls, private_pem: bytes, *, issuer: str) -> "JwtSigner":
        private_key = serialization.load_pem_private_key(private_pem, password=None)
        if not isinstance(private_key, EllipticCurvePrivateKey):
            raise ValueError("Expected EC private key for ES256")
        return cls(
            private_key=private_key,
            public_key=private_key.public_key(),
            issuer=issuer,
        )

    @classmethod
    def from_path(cls, path: str | Path, *, issuer: str) -> "JwtSigner":
        return cls.from_pem(Path(path).read_bytes(), issuer=issuer)

    @property
    def public_key_pem(self) -> bytes:
        """PEM-encoded public key. Daemon pins this at join time."""
        return self._public_pem

    def public_key_jwk(self) -> dict[str, Any]:
        """Return the public key as a JWK dict (RFC 7517). Used by the JWKS endpoint."""
        numbers = self._public_key.public_numbers()
        # P-256 coords are 32 bytes big-endian, base64url without padding
        x = numbers.x.to_bytes(32, "big")
        y = numbers.y.to_bytes(32, "big")
        return {
            "kty": "EC",
            "crv": "P-256",
            "alg": _ALGORITHM,
            "use": "sig",
            "x": base64.urlsafe_b64encode(x).rstrip(b"=").decode("ascii"),
            "y": base64.urlsafe_b64encode(y).rstrip(b"=").decode("ascii"),
        }

    def sign(self, claims: DaemonClaims, *, ttl: timedelta) -> str:
        now = datetime.now(UTC)
        payload = {
            "tenant_id": str(claims.tenant_id),
            "principal_id": str(claims.principal_id),
            "machine_id": str(claims.machine_id),
            "iss": self._issuer,
            "aud": _AUDIENCE,
            "iat": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
        }
        return pyjwt.encode(payload, self._private_pem, algorithm=_ALGORITHM)

    def verify(self, token: str) -> DaemonClaims:
        try:
            payload = pyjwt.decode(
                token,
                self._public_pem,
                algorithms=[_ALGORITHM],
                audience=_AUDIENCE,
                issuer=self._issuer,
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise JwtVerifyError("token expired") from exc
        except pyjwt.InvalidIssuerError as exc:
            raise JwtVerifyError("issuer mismatch") from exc
        except pyjwt.InvalidTokenError as exc:
            raise JwtVerifyError(f"token invalid: {exc}") from exc
        return DaemonClaims(
            tenant_id=uuid.UUID(payload["tenant_id"]),
            principal_id=uuid.UUID(payload["principal_id"]),
            machine_id=uuid.UUID(payload["machine_id"]),
        )

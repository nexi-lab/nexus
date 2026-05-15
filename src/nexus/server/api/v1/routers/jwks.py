"""/v1/.well-known/jwks.json — publish the daemon-signing public key as a JWK Set (#3804).

Lets third parties (and future non-daemon clients) verify daemon tokens without
a PEM side-channel. RFC 7517. Anonymous, cached (public key rarely rotates).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from nexus.server.api.v1.jwt_signer import JwtSigner


def make_jwks_router(*, signer: JwtSigner) -> APIRouter:
    router = APIRouter(tags=["v1", "jwks"])

    @router.get("/v1/.well-known/jwks.json")
    def jwks() -> dict[str, Any]:
        return {"keys": [signer.public_key_jwk()]}

    return router


__all__ = ["make_jwks_router"]

"""FastAPI router: POST /v1/auth/token-exchange (RFC 8693, #3818).

Verifies the daemon's JWT (subject_token), looks up the matching envelope row
via CredentialConsumer, and returns a provider-native bearer token. Errors
follow RFC 6749 §5.2 shape: ``{"error": "...", "error_description": "..."}``.

When ``enabled=False`` (default until ops verifies KMS/Vault wiring) the route
returns 501 regardless of the request — the consumer/signer args are still
required so tests and dev wiring stay symmetric.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, status
from fastapi.responses import JSONResponse

from nexus.bricks.auth.consumer import (
    AdapterMaterializeFailed,
    AuditWriteFailed,
    CredentialConsumer,
    MachineUnknownOrRevoked,
    MultipleProfilesForProvider,
    ProfileNotFoundForCaller,
    ProviderNotConfigured,
    StaleSource,
)
from nexus.bricks.auth.envelope import EncryptionProvider, EnvelopeError
from nexus.server.api.v1.jwt_signer import JwtSigner, JwtVerifyError

# RFC 6749 §5.1 — token responses MUST NOT be cached. Applied to both
# success and error responses since either may carry sensitive data.
_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

logger = logging.getLogger(__name__)

_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_SUBJECT_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
_ISSUED_TYPE = "urn:ietf:params:oauth:token-type:access_token"

_RESOURCE_TO_PROVIDER = {
    "urn:nexus:provider:aws": "aws",
    "urn:nexus:provider:github": "github",
}


def _err(http_status: int, code: str, description: str) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": code, "error_description": description},
        headers=_NO_STORE_HEADERS,
    )


def make_token_exchange_router(
    *,
    enabled: bool,
    signer: JwtSigner,
    consumer: CredentialConsumer,
    encryption: EncryptionProvider,
) -> APIRouter:
    """Build the ``/v1/auth/token-exchange`` router.

    When ``enabled=False`` the route returns 501 — gives ops a single env-var
    flag to flip the read path on/off without redeploying.
    """
    del encryption  # Reserved for future direct-decrypt fallbacks; unused for now.
    router = APIRouter(prefix="/v1/auth", tags=["auth"])

    @router.post("/token-exchange")
    def exchange(
        grant_type: str = Form(...),
        subject_token: str = Form(...),
        subject_token_type: str = Form(...),
        resource: str = Form(...),
        scope: str = Form(...),
        audience: str | None = Form(None),
        nexus_force_refresh: str = Form("false"),
    ) -> Any:
        if not enabled:
            return _err(
                status.HTTP_501_NOT_IMPLEMENTED,
                "not_implemented",
                "token-exchange disabled (NEXUS_TOKEN_EXCHANGE_ENABLED=0)",
            )

        del audience  # MVP ignores audience field (always bound by JWT verify).

        if grant_type != _GRANT_TYPE:
            return _err(400, "invalid_request", f"unknown grant_type: {grant_type!r}")
        if subject_token_type != _SUBJECT_TYPE_JWT:
            return _err(
                400, "invalid_request", f"unsupported subject_token_type: {subject_token_type!r}"
            )
        provider = _RESOURCE_TO_PROVIDER.get(resource)
        if provider is None:
            return _err(400, "invalid_request", f"unknown resource: {resource!r}")

        try:
            claims = signer.verify(subject_token)
        except JwtVerifyError as exc:
            return _err(401, "invalid_token", str(exc))

        force_refresh = nexus_force_refresh.lower() in ("1", "true", "yes")

        try:
            cred = consumer.resolve(
                claims=claims,
                provider=provider,
                purpose=scope,
                force_refresh=force_refresh,
            )
        except MachineUnknownOrRevoked as exc:
            # Cryptographically valid JWT but the daemon row is missing or
            # revoked — treat as 401 invalid_token so a compromised daemon's
            # JWT stops working the moment its row is revoked, even if the
            # JWT has not yet expired.
            return _err(401, "invalid_token", exc.cause or "machine_revoked")
        except (ProfileNotFoundForCaller, ProviderNotConfigured) as exc:
            return _err(403, "access_denied", exc.cause or "")
        except MultipleProfilesForProvider as exc:
            return _err(409, "ambiguous_profile", exc.cause or "")
        except StaleSource as exc:
            return _err(409, "stale_source", exc.cause or "")
        except AuditWriteFailed as exc:
            # Cache-miss audit could not be written. Refusing the credential
            # avoids a forensics blind spot — the operator must clear the
            # audit-table failure (partition exhaustion, RLS misconfig, etc)
            # before reads resume.
            logger.error("audit_write_failed on cache miss: %r", exc)
            return _err(503, "audit_unavailable", exc.cause or "")
        except (AdapterMaterializeFailed, EnvelopeError) as exc:
            logger.warning("envelope_error: %r", exc)  # __repr__ masks plaintext
            return _err(500, "envelope_error", "see server logs")

        # RFC 6749 §5.1: expires_in is OPTIONAL. Omit it for non-expiring
        # credentials (e.g. GitHub classic PATs) — emitting expires_in=0
        # signals "already expired" and clients drop the credential.
        body: dict[str, object] = {
            "access_token": cred.access_token,
            "issued_token_type": _ISSUED_TYPE,
            "token_type": "Bearer",
            "nexus_credential_metadata": cred.metadata,
        }
        if cred.expires_at is not None:
            body["expires_in"] = max(0, int((cred.expires_at - datetime.now(UTC)).total_seconds()))

        return JSONResponse(content=body, headers=_NO_STORE_HEADERS)

    return router

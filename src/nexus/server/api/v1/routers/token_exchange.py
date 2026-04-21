"""FastAPI router: POST /v1/auth/token-exchange (RFC 8693 stub, #3804).

This route exposes the RFC 8693 OAuth 2.0 Token Exchange contract so that
client code (daemon, admin CLI, agents) can be written against the wire
shape now, while the full implementation is deferred to a follow-up. For
the MVP the handler always returns HTTP 501, regardless of the ``enabled``
flag — the flag is threaded through from ``create_app`` via the
``NEXUS_TOKEN_EXCHANGE_ENABLED`` env var so the follow-up can flip it on
without another router signature change.

See epic #3788 for the RFC 8693 implementation plan.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel


class TokenExchangeRequest(BaseModel):
    grant_type: str
    subject_token: str
    subject_token_type: str
    resource: str | None = None
    scope: str | None = None
    audience: str | None = None


class TokenExchangeResponse(BaseModel):
    access_token: str
    issued_token_type: str
    token_type: str
    expires_in: int


def make_token_exchange_router(*, enabled: bool) -> APIRouter:
    """Build the ``/v1/auth/token-exchange`` router.

    Parameters
    ----------
    enabled:
        Reserved for future flag-gating. For this MVP both flag states
        return 501; the argument is preserved so the follow-up that
        implements RFC 8693 can switch behaviour without changing the
        public surface.
    """
    del enabled  # Reserved; both flag states return 501 for the MVP.
    router = APIRouter(prefix="/v1/auth", tags=["auth"])

    @router.post(
        "/token-exchange",
        response_model=TokenExchangeResponse,
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
    )
    def exchange(req: TokenExchangeRequest) -> TokenExchangeResponse:
        del req  # Unused — handler is a stub.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "token exchange deferred to follow-up; "
                "see epic #3788 for RFC 8693 implementation plan"
            ),
        )

    return router

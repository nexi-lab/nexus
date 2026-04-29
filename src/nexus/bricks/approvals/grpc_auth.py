"""CapabilityAuth implementations for the ApprovalsV1 gRPC servicer.

For Phase 20 (Issue #3790) E2E ergonomics we ship a simple bearer-token
auth: a single admin token, configured via ``NEXUS_APPROVALS_ADMIN_TOKEN``,
authorizes every approvals capability (``approvals:read``,
``approvals:decide``, ``approvals:request``).

TODO(#3790): wire this to the ReBAC ``AdminCapability`` system in
``nexus.bricks.rebac.permissions_enhanced`` so individual subjects can be
granted scoped approvals capabilities (e.g. a "zone operator" token that
can decide but not list across zones). For now the admin-token path keeps
E2E reachable without that mapping work.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import grpc

if TYPE_CHECKING:
    import grpc.aio

logger = logging.getLogger(__name__)

_BEARER_PREFIX = "bearer "


class BearerTokenCapabilityAuth:
    """Validate ``Bearer <token>`` metadata against a single admin token.

    Conforms to the ``CapabilityAuth`` Protocol in
    ``nexus.bricks.approvals.grpc_server``: ``authorize`` returns the
    caller's token id (used as ``decided_by`` / ``token_id``) on success
    and aborts the gRPC context with ``UNAUTHENTICATED`` on failure.

    Args:
        admin_token: A non-empty shared secret. Callers MUST send
            ``authorization: Bearer <admin_token>`` in gRPC metadata.
            An empty string raises ``ValueError`` — wire it to ``None``
            checks at the lifespan layer instead.
    """

    def __init__(self, admin_token: str) -> None:
        if not admin_token:
            raise ValueError("admin_token must be a non-empty string")
        self._admin_token = admin_token

    async def authorize(self, context: "grpc.aio.ServicerContext", capability: str) -> str:
        # gRPC metadata header names are lowercased on the wire; HTTP/2
        # canonicalizes to lowercase. Iterate defensively in case a client
        # sends a different case.
        metadata = context.invocation_metadata() or ()
        authz_value: str | None = None
        for key, value in metadata:
            if key.lower() == "authorization":
                authz_value = value
                break

        if authz_value is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "missing authorization metadata",
            )
            raise  # unreachable; abort raises. Keeps mypy happy on flow.

        # Case-insensitive scheme match; preserve the token portion exactly.
        if authz_value[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "authorization must use Bearer scheme",
            )
            raise  # unreachable

        token = authz_value[len(_BEARER_PREFIX) :].strip()
        if not token or token != self._admin_token:
            # Don't leak which half of the check failed — a wrong token and
            # a missing token look identical from the client's POV.
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "invalid bearer token",
            )
            raise  # unreachable

        # The capability string is unused under the admin-token model: any
        # admin token implicitly has every approvals capability. Logging it
        # at debug helps audit-log forwards without growing per-call cost.
        logger.debug("approvals.auth granted capability=%s", capability)
        return f"admin:{token[:8]}"

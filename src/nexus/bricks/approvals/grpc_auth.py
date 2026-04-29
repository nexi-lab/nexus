"""CapabilityAuth implementations for the ApprovalsV1 gRPC servicer.

Two implementations live here:

  - :class:`BearerTokenCapabilityAuth` — single shared admin token,
    configured via ``NEXUS_APPROVALS_ADMIN_TOKEN``. Authorizes every
    approvals capability for any caller that presents the token. Kept
    for E2E ergonomics and as a fallback under the composite auth.

  - :class:`ReBACCapabilityAuth` — resolves the bearer token through
    the standard auth pipeline (``AuthService.authenticate``) and then
    runs a ReBAC permission check against the ``approvals`` namespace
    for the requested capability. Falls through to a wrapped
    :class:`BearerTokenCapabilityAuth` when:

      * the token does not resolve to an authenticated subject AND an
        admin fallback is configured, or
      * the resolved subject is flagged ``is_admin`` (admin bypass —
        same model as the rest of the auth pipeline).

The capability mapping for ReBAC checks is intentionally narrow so the
servicer doesn't grow new permission strings ad-hoc:

    approvals:read    -> ReBAC ``read``   on ``("approvals", "global")``
    approvals:decide  -> ReBAC ``write``  on ``("approvals", "global")``
    approvals:request -> ReBAC ``create`` on ``("approvals", "global")``

Operators grant per-subject access by writing a tuple of the form
``(user, alice) -- read --> (approvals, global)`` (etc.) into the ReBAC
store. The ``approvals`` namespace is treated as a flat resource — zone
scoping happens at the row level inside :class:`ApprovalService`, not at
the auth boundary, so a single namespace tuple suffices.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

import grpc

if TYPE_CHECKING:
    import grpc.aio

    from nexus.bricks.auth.types import AuthResult

logger = logging.getLogger(__name__)

_BEARER_PREFIX = "bearer "

# ReBAC mapping for the three approvals capability strings used by
# ApprovalsServicer. Kept as a module-level constant so tests and operators
# can introspect the mapping without import-cycling through grpc_server.
_CAPABILITY_TO_PERMISSION: dict[str, str] = {
    "approvals:read": "read",
    "approvals:decide": "write",
    "approvals:request": "create",
}

# The ReBAC object that auth checks resolve against. The approvals namespace
# is flat (zone scoping is enforced at the row level inside ApprovalService),
# so a single ("approvals", "global") tuple is sufficient. Tests and
# operators can grant a subject any of read/write/create on this object.
_APPROVALS_OBJECT: tuple[str, str] = ("approvals", "global")


class _AuthLike(Protocol):
    """Minimal duck-type for the auth resolver.

    Matches both ``nexus.bricks.auth.service.AuthService`` (cache-aware
    wrapper) and the bare :class:`AuthProvider` ABCs — anything with an
    ``authenticate(token)`` coroutine returning an ``AuthResult``-shaped
    object will work.
    """

    async def authenticate(self, token: str) -> "AuthResult": ...


class _ReBACLike(Protocol):
    """Minimal duck-type for the ReBAC manager.

    Matches ``nexus.bricks.rebac.manager.ReBACManager.rebac_check``. We
    only need the synchronous form; ApprovalsServicer awaits authorize()
    so the tiny in-memory check is fine to call directly off the event
    loop for now (single SQL query at worst).
    """

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool: ...


def _extract_bearer_token(metadata: tuple[tuple[str, str], ...]) -> str | None:
    """Pull the bearer token out of gRPC metadata, or return None.

    Returns the raw token (no prefix, stripped) on success; ``None`` if
    the header is missing, empty, or doesn't use the Bearer scheme. The
    callers of this helper translate ``None`` into the right gRPC status
    code via ``context.abort``.
    """
    authz_value: str | None = None
    for key, value in metadata:
        if key.lower() == "authorization":
            authz_value = value
            break
    if authz_value is None:
        return None
    if authz_value[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    token = authz_value[len(_BEARER_PREFIX) :].strip()
    return token or None


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


class ReBACCapabilityAuth:
    """Capability auth backed by the auth pipeline + ReBAC permission graph.

    Per-RPC flow:

      1. Pull the bearer token from gRPC metadata. Missing/non-Bearer →
         abort ``UNAUTHENTICATED``.
      2. Resolve the token via ``AuthService.authenticate``. If the token
         does not resolve to an authenticated subject:

           * with an ``admin_fallback`` configured: delegate to it
             (preserves the ``NEXUS_APPROVALS_ADMIN_TOKEN`` shim path).
           * without a fallback: abort ``UNAUTHENTICATED``.

      3. If the resolved subject is ``is_admin``, grant the capability
         and return its subject id (admin bypass — the auth pipeline is
         the single source of truth for "global admin" today).
      4. Otherwise, run ``rebac_check(subject, permission, ("approvals",
         "global"))`` for the mapped permission. On success return the
         subject id; on failure abort ``PERMISSION_DENIED``.

    The capability strings ApprovalsServicer passes today are:
    ``approvals:read``, ``approvals:decide``, ``approvals:request``.
    Anything else aborts ``PERMISSION_DENIED`` with an explicit
    "unknown capability" message — fail-closed by design.

    Args:
        auth_service: Object with ``async authenticate(token) -> AuthResult``.
            Typically the ``AuthService`` instance from ``app.state``; the
            bare ``AuthProvider`` ABC is also accepted (same shape).
        rebac_manager: Object with ``rebac_check(subject, permission,
            object, zone_id=...) -> bool``. Typically
            ``app.state.rebac_manager``.
        admin_fallback: Optional :class:`BearerTokenCapabilityAuth`. When
            present, tokens that don't resolve to an auth subject are
            tried against the admin shim before being rejected. Set to
            ``None`` to enforce strict ReBAC.
    """

    def __init__(
        self,
        auth_service: _AuthLike,
        rebac_manager: _ReBACLike,
        admin_fallback: BearerTokenCapabilityAuth | None = None,
    ) -> None:
        self._auth = auth_service
        self._rebac = rebac_manager
        self._admin_fallback = admin_fallback

    async def authorize(self, context: "grpc.aio.ServicerContext", capability: str) -> str:
        metadata = context.invocation_metadata() or ()
        token = _extract_bearer_token(metadata)
        if token is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "missing or malformed authorization metadata",
            )
            raise  # unreachable; abort raises.

        # Stage 1: resolve token -> subject via the standard auth pipeline.
        # Never let auth-pipeline exceptions surface as UNKNOWN — log and
        # treat as "not resolved" so the admin-fallback branch can run.
        result: "AuthResult | None" = None
        try:
            result = await self._auth.authenticate(token)
        except Exception:  # auth pipeline boundary — defensive log, no abort
            logger.warning(
                "approvals.auth: authenticate() raised; treating as unauthenticated",
                exc_info=True,
            )
            result = None

        if result is None or not getattr(result, "authenticated", False):
            # Stage 2 (fallback): try the admin-token shim. This delegates
            # the whole authorize() — including its own abort semantics —
            # which keeps a single owner of the UNAUTHENTICATED status.
            if self._admin_fallback is not None:
                return await self._admin_fallback.authorize(context, capability)
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "invalid bearer token",
            )
            raise  # unreachable

        subject_id = getattr(result, "subject_id", None)
        if not subject_id:
            # Authenticated but identity-less — should not happen for
            # well-formed providers but treat defensively.
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "authenticated subject is missing subject_id",
            )
            raise  # unreachable

        subject_type = getattr(result, "subject_type", "user") or "user"
        is_admin = bool(getattr(result, "is_admin", False))

        # Admin bypass — auth pipeline already told us this caller has
        # global admin rights. Audit-log it at info so the bypass is
        # observable.
        if is_admin:
            logger.info(
                "approvals.auth admin bypass capability=%s subject=%s/%s",
                capability,
                subject_type,
                subject_id,
            )
            return str(subject_id)

        # Stage 3: ReBAC capability check.
        permission = _CAPABILITY_TO_PERMISSION.get(capability)
        if permission is None:
            # Unknown capability string — fail closed. ApprovalsServicer
            # only passes the three known strings today; anything else is
            # an invariant violation, not a misconfigured client.
            logger.warning(
                "approvals.auth: unknown capability %r requested by subject=%s/%s",
                capability,
                subject_type,
                subject_id,
            )
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                f"unknown capability: {capability}",
            )
            raise  # unreachable

        try:
            allowed = self._rebac.rebac_check(
                subject=(subject_type, str(subject_id)),
                permission=permission,
                object=_APPROVALS_OBJECT,
                zone_id=None,
            )
        except Exception:
            # ReBAC errors are not "no" — they're indeterminate. Fail
            # closed (PERMISSION_DENIED) but log the cause so operators
            # can tell the difference between "denied" and "broken graph".
            logger.exception(
                "approvals.auth: rebac_check raised for subject=%s/%s capability=%s",
                subject_type,
                subject_id,
                capability,
            )
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "permission check failed",
            )
            raise  # unreachable

        if not allowed:
            logger.debug(
                "approvals.auth denied capability=%s subject=%s/%s",
                capability,
                subject_type,
                subject_id,
            )
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                f"subject lacks capability: {capability}",
            )
            raise  # unreachable

        logger.debug(
            "approvals.auth granted capability=%s subject=%s/%s (rebac)",
            capability,
            subject_type,
            subject_id,
        )
        return str(subject_id)

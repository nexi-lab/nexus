"""CapabilityAuth implementations for the ApprovalsV1 gRPC servicer.

Two implementations live here:

  - :class:`BearerTokenCapabilityAuth` — single shared admin token,
    configured via ``NEXUS_APPROVALS_ADMIN_TOKEN``. Authorizes every
    approvals capability for any caller that presents the token. Kept
    for E2E ergonomics and as a fallback under the composite auth.

  - :class:`ReBACCapabilityAuth` — resolves the bearer token through
    the standard auth pipeline (``AuthService.authenticate``) and then
    runs a ReBAC permission check against the per-zone ``approvals``
    object for the requested capability. Falls through to a wrapped
    :class:`BearerTokenCapabilityAuth` when:

      * the token does not resolve to an authenticated subject AND an
        admin fallback is configured, or
      * the resolved subject is flagged ``is_admin`` (admin bypass —
        same model as the rest of the auth pipeline).

The capability mapping for ReBAC checks is intentionally narrow so the
servicer doesn't grow new permission strings ad-hoc:

    approvals:read    -> ReBAC ``read``   on ``("approvals", <zone_id>)``
    approvals:decide  -> ReBAC ``write``  on ``("approvals", <zone_id>)``
    approvals:request -> ReBAC ``create`` on ``("approvals", <zone_id>)``

Operators grant per-subject access by writing a tuple of the form
``(user, alice) -- read --> (approvals, <zone_id>)`` (etc.) into the
ReBAC store. The ``approvals`` namespace object id is the zone the
caller is acting in — this keeps capability grants strictly scoped per
zone so a leaked ``approvals:read`` for ``z1`` cannot be used to read
``z2`` rows.

Two entry points are exposed on every implementation:

  - ``authorize(ctx, capability, zone_id)`` — abort the gRPC context on
    failure (UNAUTHENTICATED for unresolved tokens, PERMISSION_DENIED
    for ReBAC denials). Used by ListPending/Watch/Submit which are
    keyed off a caller-supplied zone.
  - ``check_capability(ctx, capability, zone_id)`` — returns the
    caller's subject id on success, or ``None`` on a ReBAC denial.
    Still aborts UNAUTHENTICATED on bad/missing tokens. Used by
    Get/Decide/Cancel so the servicer can fold a denial into NOT_FOUND
    (so request_id existence does not leak across zones).
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


def _approvals_object_for_zone(zone_id: str) -> tuple[str, str]:
    """Return the per-zone ReBAC object tuple for an approvals capability check.

    The approvals namespace is registered with object_type ``approvals``
    (see ``DEFAULT_APPROVALS_NAMESPACE``), and capability grants are
    keyed off the zone the caller is acting in. A subject granted
    ``viewer`` on ``("approvals", "z1")`` can read approvals in zone
    ``z1`` but NOT in zone ``z2`` — even with the same token.

    Empty zone_id is rejected at the servicer (``ListPending``/``Watch``
    abort INVALID_ARGUMENT before we get here). For ``Get``/``Decide``/
    ``Cancel`` the row's own zone is used; if the row is missing the
    servicer aborts NOT_FOUND before this helper is called.
    """
    return ("approvals", zone_id)


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

    async def authorize(
        self,
        context: "grpc.aio.ServicerContext",
        capability: str,
        zone_id: str,
    ) -> str:
        # ``zone_id`` is intentionally accepted-and-ignored here: the
        # admin-token shim is a global bypass — any caller presenting
        # the configured admin token authorizes for every zone. We
        # accept the parameter so callers (ApprovalsServicer) can pass
        # the zone uniformly without branching on auth backend.
        del zone_id  # admin token is global; see docstring.

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

    async def check_capability(
        self,
        context: "grpc.aio.ServicerContext",
        capability: str,
        zone_id: str,
    ) -> str | None:
        """Non-aborting variant of ``authorize`` — admin token always wins.

        Bad/missing tokens still abort UNAUTHENTICATED (those cases
        cannot be legitimately distinguished from "wrong zone"); a valid
        admin token returns the admin subject id. Symmetrical with
        ``ReBACCapabilityAuth.check_capability`` so the servicer can use
        either backend uniformly.
        """
        return await self.authorize(context, capability, zone_id)

    async def authenticate_only(
        self,
        context: "grpc.aio.ServicerContext",
    ) -> str:
        """F2 (#3790): bearer-token-only validation, no ReBAC.

        Used by Get/Decide/Cancel before the row lookup so an
        unauthenticated caller cannot use the response code to probe
        whether a given request_id exists. Aborts UNAUTHENTICATED on
        missing/bad tokens; returns the admin subject id otherwise.
        ``capability``/``zone_id`` are intentionally unused — the
        admin-token shim is global, the servicer's per-row
        ``check_capability`` call still runs after the row is fetched.
        """
        # Reuse authorize()'s validation; pass placeholder values for
        # capability/zone — the admin shim ignores both.
        return await self.authorize(context, "approvals:authenticate", "")


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
         zone_id))`` for the mapped permission. On success return the
         subject id; on failure abort ``PERMISSION_DENIED``.

    The capability strings ApprovalsServicer passes today are:
    ``approvals:read``, ``approvals:decide``, ``approvals:request``.
    Anything else aborts ``PERMISSION_DENIED`` with an explicit
    "unknown capability" message — fail-closed by design.

    The ``zone_id`` parameter is mandatory: the servicer either passes
    the request's ``zone_id`` (ListPending/Watch/Submit) or the
    resolved row's ``zone_id`` (Get/Decide/Cancel — fetched first).
    Empty zone aborts INVALID_ARGUMENT at the servicer before reaching
    this layer.

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

    async def authorize(
        self,
        context: "grpc.aio.ServicerContext",
        capability: str,
        zone_id: str,
    ) -> str:
        result = await self._check_capability_inner(context, capability, zone_id)
        if result is None:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                f"subject lacks capability: {capability}",
            )
            raise  # unreachable.
        return result

    async def check_capability(
        self,
        context: "grpc.aio.ServicerContext",
        capability: str,
        zone_id: str,
    ) -> str | None:
        """Same as ``authorize`` but returns ``None`` on ReBAC denial.

        Bad/missing tokens still abort UNAUTHENTICATED — those cases
        are not "wrong zone" and must not be foldable into NOT_FOUND.
        Used by ApprovalsServicer for Get/Decide/Cancel where a denial
        must be presented to the caller as NOT_FOUND so request_id
        existence does not leak across zones.
        """
        return await self._check_capability_inner(context, capability, zone_id)

    async def authenticate_only(
        self,
        context: "grpc.aio.ServicerContext",
    ) -> str:
        """F2 (#3790): bearer-token-only validation, no ReBAC.

        Used by Get/Decide/Cancel BEFORE the row lookup so an
        unauthenticated caller cannot use the response code to probe
        whether a given request_id exists. Aborts UNAUTHENTICATED on
        missing/bad tokens; returns the resolved subject id otherwise.
        Falls through to the admin-token shim (when configured) and
        honors the ``is_admin`` bypass — symmetric with the rest of
        the auth pipeline.
        """
        metadata = context.invocation_metadata() or ()
        token = _extract_bearer_token(metadata)
        if token is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "missing or malformed authorization metadata",
            )
            raise  # unreachable; abort raises.

        # Stage 1: resolve token -> subject via the standard auth pipeline.
        result: "AuthResult | None" = None
        try:
            result = await self._auth.authenticate(token)
        except Exception:  # auth pipeline boundary — defensive log, no abort
            logger.warning(
                "approvals.auth: authenticate() raised in authenticate_only; "
                "treating as unauthenticated",
                exc_info=True,
            )
            result = None

        if result is None or not getattr(result, "authenticated", False):
            # Stage 2 (fallback): admin-token shim — same pattern as
            # _check_capability_inner. Delegating to the shim's
            # ``authenticate_only`` keeps a single owner of the
            # UNAUTHENTICATED status code.
            if self._admin_fallback is not None:
                return await self._admin_fallback.authenticate_only(context)
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "invalid bearer token",
            )
            raise  # unreachable

        subject_id = getattr(result, "subject_id", None)
        if not subject_id:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "authenticated subject is missing subject_id",
            )
            raise  # unreachable
        return str(subject_id)

    async def _check_capability_inner(
        self,
        context: "grpc.aio.ServicerContext",
        capability: str,
        zone_id: str,
    ) -> str | None:
        """Shared body of ``authorize``/``check_capability``.

        Returns the subject id on success, ``None`` on a ReBAC-level
        denial (unknown capability, missing grant, rebac_check raised).
        Aborts the context on UNAUTHENTICATED conditions (bad token).
        """
        # Defensive guard: the servicer should reject empty zone_id
        # before reaching us, but fail-closed if it ever slips through.
        if not zone_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "zone_id is required for capability check",
            )
            raise  # unreachable; abort raises.

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
                return await self._admin_fallback.authorize(context, capability, zone_id)
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
                "approvals.auth admin bypass capability=%s subject=%s/%s zone=%s",
                capability,
                subject_type,
                subject_id,
                zone_id,
            )
            return str(subject_id)

        # Stage 3: ReBAC capability check (zone-scoped object).
        permission = _CAPABILITY_TO_PERMISSION.get(capability)
        if permission is None:
            # Unknown capability string — fail closed. ApprovalsServicer
            # only passes the three known strings today; anything else is
            # an invariant violation, not a misconfigured client. Surface
            # as a ReBAC-level denial (None) — authorize() will translate
            # to PERMISSION_DENIED and the caller will see "unknown
            # capability".
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

        approvals_object = _approvals_object_for_zone(zone_id)
        try:
            allowed = self._rebac.rebac_check(
                subject=(subject_type, str(subject_id)),
                permission=permission,
                object=approvals_object,
                zone_id=zone_id,
            )
        except Exception:
            # ReBAC errors are not "no" — they're indeterminate. Fail
            # closed but log the cause so operators can tell the
            # difference between "denied" and "broken graph".
            logger.exception(
                "approvals.auth: rebac_check raised for subject=%s/%s capability=%s zone=%s",
                subject_type,
                subject_id,
                capability,
                zone_id,
            )
            return None

        if not allowed:
            logger.debug(
                "approvals.auth denied capability=%s subject=%s/%s zone=%s",
                capability,
                subject_type,
                subject_id,
                zone_id,
            )
            return None

        logger.debug(
            "approvals.auth granted capability=%s subject=%s/%s zone=%s (rebac)",
            capability,
            subject_type,
            subject_id,
            zone_id,
        )
        return str(subject_id)

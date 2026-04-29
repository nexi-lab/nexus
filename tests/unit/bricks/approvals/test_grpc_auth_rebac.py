"""Unit tests for ReBACCapabilityAuth (Issue #3790).

Validates the contract documented in ``grpc_auth.py``:

  - token resolves to authenticated subject + has-capability →
    returns subject_id
  - token resolves to authenticated subject + lacks capability →
    aborts PERMISSION_DENIED
  - admin subject (is_admin=True) bypasses ReBAC → returns subject_id
  - token does not resolve + admin_fallback configured → fallback
    handles it
  - token does not resolve + no admin_fallback → aborts UNAUTHENTICATED
  - missing/malformed authorization metadata → aborts UNAUTHENTICATED
  - unknown capability string → aborts PERMISSION_DENIED
  - rebac_check raises → aborts PERMISSION_DENIED (fail-closed)
  - capability → permission mapping is correct for the three approvals
    capability strings the servicer passes today

The mocks intentionally implement only the small surface
``ReBACCapabilityAuth`` actually touches (``authenticate``,
``rebac_check``) so tests stay fast and don't require Postgres / a
populated ReBAC graph.
"""

from __future__ import annotations

from typing import Any, cast

import grpc
import grpc.aio
import pytest

from nexus.bricks.approvals.grpc_auth import (
    _APPROVALS_OBJECT,
    _CAPABILITY_TO_PERMISSION,
    BearerTokenCapabilityAuth,
    ReBACCapabilityAuth,
)
from nexus.bricks.auth.types import AuthResult

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal grpc.aio.ServicerContext stand-in.

    Records ``invocation_metadata`` and ``abort`` calls so we can assert
    against them. ``abort`` raises an exception (matching the real
    grpc.aio behavior) so the rest of the production code path is exited
    via the standard control flow.
    """

    def __init__(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self._metadata = metadata
        self.aborted_with: tuple[grpc.StatusCode, str] | None = None

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata

    async def abort(self, code: grpc.StatusCode, details: str) -> Any:
        self.aborted_with = (code, details)
        raise RuntimeError(f"abort:{code.name}:{details}")


def _ctx(metadata: tuple[tuple[str, str], ...]) -> tuple[_FakeContext, "grpc.aio.ServicerContext"]:
    """Build a fake context and its grpc.aio.ServicerContext-typed view."""
    fake = _FakeContext(metadata=metadata)
    return fake, cast("grpc.aio.ServicerContext", fake)


class _FakeAuth:
    """Minimal AuthService stand-in: returns whatever was queued for a token."""

    def __init__(self, results: dict[str, AuthResult]) -> None:
        self._results = results
        self.calls: list[str] = []
        self.raise_for: set[str] = set()

    async def authenticate(self, token: str) -> AuthResult:
        self.calls.append(token)
        if token in self.raise_for:
            raise RuntimeError("simulated auth pipeline failure")
        return self._results.get(token, AuthResult(authenticated=False))


class _FakeReBAC:
    """Minimal ReBACManager stand-in: rule-based allow/deny."""

    def __init__(self, allow: set[tuple[tuple[str, str], str, tuple[str, str]]] | None = None):
        self.allow = allow or set()
        self.calls: list[tuple[tuple[str, str], str, tuple[str, str]]] = []
        self.raise_on_call = False

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],  # noqa: A002 - matches ReBACManager API
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        self.calls.append((subject, permission, object))
        if self.raise_on_call:
            raise RuntimeError("simulated rebac failure")
        return (subject, permission, object) in self.allow


# ---------------------------------------------------------------------------
# Capability mapping invariants
# ---------------------------------------------------------------------------


def test_capability_mapping_covers_servicer_strings() -> None:
    """The three capability strings ApprovalsServicer passes must be mapped."""
    assert _CAPABILITY_TO_PERMISSION["approvals:read"] == "read"
    assert _CAPABILITY_TO_PERMISSION["approvals:decide"] == "write"
    assert _CAPABILITY_TO_PERMISSION["approvals:request"] == "create"
    assert _APPROVALS_OBJECT == ("approvals", "global")


# ---------------------------------------------------------------------------
# Bearer header parsing — shared with the admin-token shim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_authorization_metadata_aborts_unauth() -> None:
    auth = ReBACCapabilityAuth(
        auth_service=_FakeAuth({}),
        rebac_manager=_FakeReBAC(),
        admin_fallback=None,
    )
    fake, ctx = _ctx(())

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_non_bearer_scheme_aborts_unauth() -> None:
    auth = ReBACCapabilityAuth(
        auth_service=_FakeAuth({}),
        rebac_manager=_FakeReBAC(),
        admin_fallback=None,
    )
    fake, ctx = _ctx((("authorization", "Basic dXNlcjpwYXNz"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


# ---------------------------------------------------------------------------
# Token resolves -> ReBAC check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_subject_with_capability_returns_subject_id() -> None:
    fake_auth = _FakeAuth(
        {
            "tok-alice": AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="alice",
            )
        }
    )
    rebac = _FakeReBAC(
        allow={
            (("user", "alice"), "read", ("approvals", "global")),
        }
    )
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    fake, ctx = _ctx((("authorization", "Bearer tok-alice"),))

    subject = await auth.authorize(ctx, "approvals:read")

    assert subject == "alice"
    assert fake.aborted_with is None
    assert fake_auth.calls == ["tok-alice"]
    assert rebac.calls == [(("user", "alice"), "read", ("approvals", "global"))]


@pytest.mark.asyncio
async def test_resolved_subject_without_capability_aborts_permission_denied() -> None:
    fake_auth = _FakeAuth(
        {
            "tok-bob": AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="bob",
            )
        }
    )
    rebac = _FakeReBAC(allow=set())  # bob has no grants
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    fake, ctx = _ctx((("authorization", "Bearer tok-bob"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:decide")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED
    # Capability mapping validated: decide -> write
    assert rebac.calls == [(("user", "bob"), "write", ("approvals", "global"))]


@pytest.mark.asyncio
async def test_request_capability_maps_to_create_permission() -> None:
    fake_auth = _FakeAuth(
        {
            "tok-carol": AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="carol",
            )
        }
    )
    rebac = _FakeReBAC(
        allow={
            (("user", "carol"), "create", ("approvals", "global")),
        }
    )
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    _fake, ctx = _ctx((("authorization", "Bearer tok-carol"),))

    subject = await auth.authorize(ctx, "approvals:request")

    assert subject == "carol"
    assert rebac.calls == [(("user", "carol"), "create", ("approvals", "global"))]


@pytest.mark.asyncio
async def test_admin_subject_bypasses_rebac() -> None:
    """``is_admin=True`` from the auth pipeline grants every capability."""
    fake_auth = _FakeAuth(
        {
            "tok-root": AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="root",
                is_admin=True,
            )
        }
    )
    rebac = _FakeReBAC(allow=set())  # would otherwise deny
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    fake, ctx = _ctx((("authorization", "Bearer tok-root"),))

    subject = await auth.authorize(ctx, "approvals:decide")

    assert subject == "root"
    assert fake.aborted_with is None
    # ReBAC must not be consulted on admin bypass.
    assert rebac.calls == []


@pytest.mark.asyncio
async def test_unknown_capability_aborts_permission_denied() -> None:
    fake_auth = _FakeAuth(
        {
            "tok-dave": AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="dave",
            )
        }
    )
    rebac = _FakeReBAC()
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    fake, ctx = _ctx((("authorization", "Bearer tok-dave"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:nuke-everything")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED
    # ReBAC must not be consulted for an unknown capability.
    assert rebac.calls == []


@pytest.mark.asyncio
async def test_rebac_raises_aborts_permission_denied_fail_closed() -> None:
    fake_auth = _FakeAuth(
        {
            "tok-eve": AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="eve",
            )
        }
    )
    rebac = _FakeReBAC()
    rebac.raise_on_call = True
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    fake, ctx = _ctx((("authorization", "Bearer tok-eve"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED


# ---------------------------------------------------------------------------
# Fall-through to admin-token shim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_token_falls_through_to_admin_fallback() -> None:
    """When the token doesn't resolve to a subject, try the admin shim."""
    fake_auth = _FakeAuth({})  # no token resolves
    rebac = _FakeReBAC()
    fallback = BearerTokenCapabilityAuth(admin_token="adm-secret-12345678")
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=fallback,
    )
    fake, ctx = _ctx((("authorization", "Bearer adm-secret-12345678"),))

    subject = await auth.authorize(ctx, "approvals:read")

    # Admin shim returns its own ``admin:<prefix>`` subject id.
    assert subject.startswith("admin:")
    assert fake.aborted_with is None
    assert fake_auth.calls == ["adm-secret-12345678"]
    assert rebac.calls == []


@pytest.mark.asyncio
async def test_unresolved_token_no_fallback_aborts_unauth() -> None:
    fake_auth = _FakeAuth({})
    rebac = _FakeReBAC()
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    fake, ctx = _ctx((("authorization", "Bearer mystery-token"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED
    assert rebac.calls == []


@pytest.mark.asyncio
async def test_unresolved_token_with_wrong_admin_token_aborts_unauth() -> None:
    """Fallback aborts UNAUTHENTICATED when admin token is wrong, too."""
    fake_auth = _FakeAuth({})
    rebac = _FakeReBAC()
    fallback = BearerTokenCapabilityAuth(admin_token="adm-secret")
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=fallback,
    )
    fake, ctx = _ctx((("authorization", "Bearer wrong-token"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_auth_pipeline_exception_treated_as_unresolved() -> None:
    """An auth-pipeline error must not surface — fall through to fallback."""
    fake_auth = _FakeAuth({})
    fake_auth.raise_for = {"tok-flaky"}
    rebac = _FakeReBAC()
    fallback = BearerTokenCapabilityAuth(admin_token="adm-secret-xyz")
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=fallback,
    )
    fake, ctx = _ctx((("authorization", "Bearer tok-flaky"),))

    with pytest.raises(RuntimeError):
        # tok-flaky is not the admin token, so fallback aborts UNAUTH.
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_authenticated_but_missing_subject_id_aborts_unauth() -> None:
    """Authenticated=True but no subject_id is a provider bug — fail closed."""
    fake_auth = _FakeAuth(
        {
            "tok-anon": AuthResult(authenticated=True, subject_id=None),
        }
    )
    rebac = _FakeReBAC()
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    fake, ctx = _ctx((("authorization", "Bearer tok-anon"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_subject_type_propagates_to_rebac_check() -> None:
    """Non-default subject_type (e.g. ``agent``) must reach rebac_check."""
    fake_auth = _FakeAuth(
        {
            "tok-agent": AuthResult(
                authenticated=True,
                subject_type="agent",
                subject_id="agent_42",
            )
        }
    )
    rebac = _FakeReBAC(
        allow={
            (("agent", "agent_42"), "read", ("approvals", "global")),
        }
    )
    auth = ReBACCapabilityAuth(
        auth_service=fake_auth,
        rebac_manager=rebac,
        admin_fallback=None,
    )
    _fake, ctx = _ctx((("authorization", "Bearer tok-agent"),))

    subject = await auth.authorize(ctx, "approvals:read")

    assert subject == "agent_42"
    assert rebac.calls == [(("agent", "agent_42"), "read", ("approvals", "global"))]

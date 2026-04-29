"""Unit tests for BearerTokenCapabilityAuth.

Validates the contract documented in ``grpc_auth.py``:
  - missing ``authorization`` metadata aborts with UNAUTHENTICATED
  - non-Bearer scheme aborts with UNAUTHENTICATED
  - empty token aborts with UNAUTHENTICATED
  - wrong token aborts with UNAUTHENTICATED
  - correct token returns a non-empty subject id

The authorize() Protocol uses ``await context.abort(...)`` to fail; abort
raises ``grpc.aio.AbortError`` after recording the status — we assert on
the recorded status code, which matches what production callers observe.
"""

from __future__ import annotations

from typing import Any, cast

import grpc
import grpc.aio
import pytest

from nexus.bricks.approvals.grpc_auth import BearerTokenCapabilityAuth


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
    """Build a fake context and its grpc.aio.ServicerContext-typed view.

    The fake exposes only the two methods authorize() touches; the cast
    keeps mypy happy without polluting tests with type-suppression
    comments.
    """
    fake = _FakeContext(metadata=metadata)
    return fake, cast("grpc.aio.ServicerContext", fake)


def test_constructor_rejects_empty_token() -> None:
    with pytest.raises(ValueError):
        BearerTokenCapabilityAuth(admin_token="")


@pytest.mark.asyncio
async def test_missing_authorization_metadata_aborts_unauth() -> None:
    auth = BearerTokenCapabilityAuth(admin_token="s3cret")
    fake, ctx = _ctx(())

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_non_bearer_scheme_aborts_unauth() -> None:
    auth = BearerTokenCapabilityAuth(admin_token="s3cret")
    fake, ctx = _ctx((("authorization", "Basic dXNlcjpwYXNz"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_empty_token_after_bearer_aborts_unauth() -> None:
    auth = BearerTokenCapabilityAuth(admin_token="s3cret")
    fake, ctx = _ctx((("authorization", "Bearer "),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_wrong_token_aborts_unauth() -> None:
    auth = BearerTokenCapabilityAuth(admin_token="s3cret")
    fake, ctx = _ctx((("authorization", "Bearer wrongtoken"),))

    with pytest.raises(RuntimeError):
        await auth.authorize(ctx, "approvals:read")

    assert fake.aborted_with is not None
    assert fake.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_correct_token_returns_subject_id() -> None:
    auth = BearerTokenCapabilityAuth(admin_token="s3cret-12345678")
    fake, ctx = _ctx((("authorization", "Bearer s3cret-12345678"),))

    subject = await auth.authorize(ctx, "approvals:decide")

    assert fake.aborted_with is None
    assert subject.startswith("admin:")
    assert subject != "admin:"


@pytest.mark.asyncio
async def test_bearer_scheme_is_case_insensitive() -> None:
    auth = BearerTokenCapabilityAuth(admin_token="s3cret")
    fake, ctx = _ctx((("authorization", "bearer s3cret"),))
    subject = await auth.authorize(ctx, "approvals:read")
    assert fake.aborted_with is None
    assert subject.startswith("admin:")


@pytest.mark.asyncio
async def test_metadata_key_is_case_insensitive() -> None:
    auth = BearerTokenCapabilityAuth(admin_token="s3cret")
    fake, ctx = _ctx((("Authorization", "Bearer s3cret"),))
    subject = await auth.authorize(ctx, "approvals:read")
    assert fake.aborted_with is None
    assert subject.startswith("admin:")

"""Build & wire the approvals stack from configuration.

Used by the daemon/server lifespan to construct the full ApprovalService +
PolicyGate pair behind a feature flag. When ``ApprovalConfig.enabled`` is
False (the default), the returned stack carries ``service=None`` and
``gate=None`` — callers can attach this to ``app.state.policy_gate`` and
the egress/zone-access hooks treat ``None`` as "approvals disabled" by
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.events import NotifyBridge
from nexus.bricks.approvals.policy_gate import PolicyGate
from nexus.bricks.approvals.repository import ApprovalRepository
from nexus.bricks.approvals.service import ApprovalService

if TYPE_CHECKING:
    import asyncpg
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class ApprovalsStack:
    """Bundle of constructed approvals components for a single daemon process.

    The ``service`` and ``gate`` fields are both ``None`` when the brick is
    disabled via configuration. Callers MUST treat ``None`` as a signal to
    skip approvals integration entirely (no NOTIFY listener, no sweeper,
    no PolicyGate enforcement).
    """

    config: ApprovalConfig
    service: ApprovalService | None
    gate: PolicyGate | None


async def build_approvals_stack(
    config: ApprovalConfig,
    *,
    session_factory: "async_sessionmaker[AsyncSession]",
    asyncpg_pool: "asyncpg.Pool",
) -> ApprovalsStack:
    """Construct and start the approvals stack, or return a no-op stack.

    Args:
        config: ApprovalConfig — when ``enabled`` is False, returns a stack
            with both ``service`` and ``gate`` set to ``None``.
        session_factory: An ``async_sessionmaker[AsyncSession]`` for the
            ApprovalRepository (used by the ORM-bound queue tables).
        asyncpg_pool: A raw ``asyncpg.Pool`` for the NotifyBridge
            (LISTEN/NOTIFY needs a long-lived asyncpg connection).

    Returns:
        ApprovalsStack — when enabled, ``service.start()`` has already been
        awaited (NotifyBridge listening, sweeper running). Callers SHOULD
        await ``shutdown_approvals_stack(stack)`` on teardown.
    """
    if not config.enabled:
        return ApprovalsStack(config=config, service=None, gate=None)

    repo = ApprovalRepository(session_factory)
    bridge = NotifyBridge(asyncpg_pool)
    service = ApprovalService(repo, bridge, config)
    await service.start()
    gate = PolicyGate(service)
    return ApprovalsStack(config=config, service=service, gate=gate)


async def shutdown_approvals_stack(stack: ApprovalsStack) -> None:
    """Stop the approvals service if it was started.

    Idempotent / safe on a disabled stack (``service is None`` is a no-op).
    """
    if stack.service is not None:
        await stack.service.stop()

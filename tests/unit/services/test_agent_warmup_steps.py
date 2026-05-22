from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.contracts.agent_warmup_types import WarmupContext
from nexus.services.agents.warmup_steps import mount_namespace, verify_bricks


@pytest.mark.asyncio()
async def test_mount_namespace_skips_namespace_manager_without_mount_api() -> None:
    ctx = WarmupContext(
        agent_id="admin,agent",
        agent_record=SimpleNamespace(zone_id="root"),
        agent_registry=object(),
        namespace_manager=object(),
    )

    assert await mount_namespace(ctx) is True


@pytest.mark.asyncio()
async def test_verify_bricks_allows_records_without_capabilities() -> None:
    ctx = WarmupContext(
        agent_id="admin,agent",
        agent_record=SimpleNamespace(),
        agent_registry=object(),
        enabled_bricks=frozenset({"search"}),
    )

    assert await verify_bricks(ctx) is True

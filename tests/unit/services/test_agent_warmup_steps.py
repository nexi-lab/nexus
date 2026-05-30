from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.contracts.agent_warmup_types import WarmupContext
from nexus.services.agents.warmup_steps import verify_bricks


@pytest.mark.asyncio()
async def test_verify_bricks_allows_records_without_capabilities() -> None:
    ctx = WarmupContext(
        agent_id="admin,agent",
        agent_record=SimpleNamespace(),
        agent_registry=object(),
        enabled_bricks=frozenset({"search"}),
    )

    assert await verify_bricks(ctx) is True

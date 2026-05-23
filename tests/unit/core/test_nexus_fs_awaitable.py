"""Regression coverage for async ``nexus.connect(...)`` call sites."""

from __future__ import annotations

import pytest

from nexus.core.nexus_fs import NexusFS


@pytest.mark.asyncio
async def test_nexus_fs_instance_can_be_awaited_for_existing_connect_call_sites() -> None:
    """``await nexus.connect(...)`` should return the synchronous NexusFS object."""
    nx = object.__new__(NexusFS)

    assert await nx is nx

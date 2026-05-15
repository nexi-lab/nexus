"""Tests for archive scheduler."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from nexus.bricks.archive.retention import RetentionPolicy
from nexus.bricks.archive.scheduler import ArchiveScheduler, ScheduleConfig


def test_due_at_cron_matches_minute():
    cfg = ScheduleConfig(cron="0 2 * * *", policy=RetentionPolicy(7, 4, 6))
    sched = ArchiveScheduler(cfg, orchestrator=MagicMock(), storage=MagicMock())
    assert sched._is_due(datetime(2026, 5, 1, 2, 0, tzinfo=UTC))
    assert not sched._is_due(datetime(2026, 5, 1, 2, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_run_once_creates_archives_and_uploads():
    orch = MagicMock()
    orch.create_archives.return_value = []
    storage = MagicMock()
    storage.list.return_value = []
    cfg = ScheduleConfig(cron="0 2 * * *", policy=RetentionPolicy(7, 4, 6))
    sched = ArchiveScheduler(cfg, orchestrator=orch, storage=storage)
    await sched.run_once(now=datetime(2026, 5, 1, 2, 0, tzinfo=UTC))
    assert orch.create_archives.called

"""Tests for GFS retention math."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nexus.bricks.archive.retention import RetentionPolicy, apply_retention


@dataclass
class FakeEntry:
    key: str
    last_modified: datetime


def _entry(days_ago: int) -> FakeEntry:
    return FakeEntry(
        key=f"a-{days_ago}.nexus",
        last_modified=datetime(2026, 5, 1, tzinfo=UTC) - timedelta(days=days_ago),
    )


def test_keeps_n_daily_recent():
    entries = [_entry(d) for d in range(0, 30)]
    keep, prune = apply_retention(
        entries,
        RetentionPolicy(daily=7, weekly=0, monthly=0),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert len(keep) == 7
    assert all(e in entries[:7] for e in keep)


def test_keeps_one_per_iso_week_for_weekly():
    entries = [_entry(d) for d in range(0, 60)]
    keep, _prune = apply_retention(
        entries,
        RetentionPolicy(daily=0, weekly=4, monthly=0),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    iso_weeks = {e.last_modified.isocalendar()[:2] for e in keep}
    assert len(iso_weeks) == len(keep)
    assert len(keep) == 4


def test_keeps_one_per_calendar_month_for_monthly():
    entries = [_entry(d) for d in range(0, 365)]
    keep, _prune = apply_retention(
        entries,
        RetentionPolicy(daily=0, weekly=0, monthly=6),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    months = {(e.last_modified.year, e.last_modified.month) for e in keep}
    assert len(months) == len(keep)
    assert len(keep) == 6


def test_combined_policy_dedupes_overlapping():
    entries = [_entry(d) for d in range(0, 365)]
    keep, _prune = apply_retention(
        entries,
        RetentionPolicy(daily=7, weekly=4, monthly=6),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert len({e.key for e in keep}) == len(keep)
    assert len(keep) <= 7 + 4 + 6


def test_pruned_is_complement_of_keep():
    entries = [_entry(d) for d in range(0, 30)]
    keep, prune = apply_retention(
        entries,
        RetentionPolicy(daily=7, weekly=0, monthly=0),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert {e.key for e in keep} | {e.key for e in prune} == {e.key for e in entries}
    assert {e.key for e in keep} & {e.key for e in prune} == set()

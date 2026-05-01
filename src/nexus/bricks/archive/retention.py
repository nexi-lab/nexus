"""Grandfather-father-son (GFS) retention math for scheduled archives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class RetentionPolicy:
    daily: int
    weekly: int
    monthly: int


class _HasMtime(Protocol):
    last_modified: datetime
    key: str


def apply_retention(
    entries: list[_HasMtime],
    policy: RetentionPolicy,
    *,
    now: datetime,  # noqa: ARG001 — reserved for future relative-date logic
) -> tuple[list[_HasMtime], list[_HasMtime]]:
    """Return (keep, prune) lists according to GFS policy.

    Keeps the N most recent daily, then one per ISO week up to `weekly`,
    then one per calendar month up to `monthly`. The same entry can satisfy
    multiple slots; the keep set is deduped.
    """
    if not entries:
        return [], []

    sorted_desc = sorted(entries, key=lambda e: e.last_modified, reverse=True)

    keep_keys: set[str] = set()
    keep: list[_HasMtime] = []

    def _add(entry: _HasMtime) -> None:
        if entry.key not in keep_keys:
            keep_keys.add(entry.key)
            keep.append(entry)

    # Daily: keep the N most recent entries.
    for e in sorted_desc[: policy.daily]:
        _add(e)

    # Weekly: keep one entry per ISO week, up to `weekly` distinct weeks.
    seen_weeks: set[tuple[int, int]] = set()
    weekly_picks = 0
    for e in sorted_desc:
        wk = e.last_modified.isocalendar()[:2]
        if wk in seen_weeks:
            continue
        seen_weeks.add(wk)
        # Whether already kept or not, this week slot is consumed.
        if e.key not in keep_keys:
            _add(e)
        weekly_picks += 1
        if weekly_picks >= policy.weekly:
            break

    # Monthly: keep one entry per calendar month, up to `monthly` distinct months.
    seen_months: set[tuple[int, int]] = set()
    monthly_picks = 0
    for e in sorted_desc:
        m = (e.last_modified.year, e.last_modified.month)
        if m in seen_months:
            continue
        seen_months.add(m)
        # Whether already kept or not, this month slot is consumed.
        if e.key not in keep_keys:
            _add(e)
        monthly_picks += 1
        if monthly_picks >= policy.monthly:
            break

    prune = [e for e in entries if e.key not in keep_keys]
    return keep, prune


__all__ = ["RetentionPolicy", "apply_retention"]

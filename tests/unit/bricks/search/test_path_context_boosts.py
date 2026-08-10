"""Unit tests for ``prefix_boosts_from_records`` (#4620).

The P12 plugin applies ``QueryRequest.path_prefix_boosts`` post-fusion
(longest ``starts_with``-matching key wins). These tests pin the
translation from stored ``path_contexts`` rows (slash-free canonical
prefixes, optional weights) onto that wire map.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nexus.bricks.search.path_context import PathContextRecord, prefix_boosts_from_records

_NOW = datetime.now(UTC).replace(tzinfo=None)


def _record(prefix: str, weight: float | None) -> PathContextRecord:
    return PathContextRecord(
        zone_id="root",
        path_prefix=prefix,
        description="d",
        created_at=_NOW,
        updated_at=_NOW,
        weight=weight,
    )


def test_no_rows_returns_empty() -> None:
    assert prefix_boosts_from_records([]) == {}


def test_description_only_rows_return_empty() -> None:
    # Zones that only use path contexts for descriptions must keep the
    # plugin's no-boost fast path (empty map skips the scoring pass).
    records = [_record("docs", None), _record("src/nexus", None)]
    assert prefix_boosts_from_records(records) == {}


def test_all_explicit_neutral_weights_return_empty() -> None:
    # weight=1.0 is a no-op multiplier; a zone with only neutral weights
    # must not trigger the plugin's boost pass either.
    records = [_record("docs", 1.0)]
    assert prefix_boosts_from_records(records) == {}


def test_weighted_row_wrapped_to_slash_boundary_key() -> None:
    # Stored prefixes are slash-free canonical; plugin hit paths are
    # slash-prefixed and matched with plain starts_with, so the key must
    # be /-wrapped to stay on directory boundaries (no /docs-v2 leak).
    records = [_record("docs", 5.0)]
    assert prefix_boosts_from_records(records) == {"/docs/": 5.0}


def test_empty_prefix_is_zone_wide_key() -> None:
    # "" matches every path via starts_with — the zone-wide boost row.
    records = [_record("", 2.0)]
    assert prefix_boosts_from_records(records) == {"": 2.0}


def test_weightless_rows_included_as_neutral_when_any_weight_exists() -> None:
    # Pre-P12 parity: the longest matching RECORD won even when it had no
    # weight (multiplier defaulted to 1.0), shadowing shorter weighted
    # prefixes. A description-only child row must therefore ride along as
    # an explicit 1.0 so it keeps exempting its subtree from the parent's
    # boost.
    records = [_record("docs", 5.0), _record("docs/archive", None)]
    assert prefix_boosts_from_records(records) == {
        "/docs/": 5.0,
        "/docs/archive/": 1.0,
    }

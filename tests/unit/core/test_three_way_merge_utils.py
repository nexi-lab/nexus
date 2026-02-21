"""Unit tests for three-way merge utility (Issue #1273).

Tests the generic three_way_merge_dicts() function and _compute_dict_changes()
helper in nexus.lib.merge_utils.
"""

import pytest

from nexus.lib.merge_utils import _compute_dict_changes, three_way_merge_dicts


class TestComputeDictChanges:
    """Tests for _compute_dict_changes()."""

    def test_no_changes(self) -> None:
        base = {"a": 1, "b": 2}
        assert _compute_dict_changes(base, dict(base)) == {}

    def test_additions(self) -> None:
        base: dict[str, int] = {"a": 1}
        target = {"a": 1, "b": 2}
        changes = _compute_dict_changes(base, target)
        assert changes == {"b": ("add", 2)}

    def test_deletions(self) -> None:
        base = {"a": 1, "b": 2}
        target = {"a": 1}
        changes = _compute_dict_changes(base, target)
        assert changes == {"b": ("delete", None)}

    def test_modifications(self) -> None:
        base = {"a": 1, "b": 2}
        target = {"a": 1, "b": 99}
        changes = _compute_dict_changes(base, target)
        assert changes == {"b": ("modify", 99)}

    def test_mixed(self) -> None:
        base = {"a": 1, "b": 2, "c": 3}
        target = {"a": 10, "c": 3, "d": 4}
        changes = _compute_dict_changes(base, target)
        assert changes == {"a": ("modify", 10), "b": ("delete", None), "d": ("add", 4)}


class TestThreeWayMergeDicts:
    """Tests for three_way_merge_dicts()."""

    def test_all_empty(self) -> None:
        merged, conflicts = three_way_merge_dicts({}, {}, {})
        assert merged == {}
        assert conflicts == []

    def test_left_adds(self) -> None:
        base: dict[str, int] = {"a": 1}
        left = {"a": 1, "b": 2}
        right = {"a": 1}
        merged, conflicts = three_way_merge_dicts(base, left, right)
        assert merged == {"a": 1, "b": 2}
        assert conflicts == []

    def test_right_adds(self) -> None:
        base: dict[str, int] = {"a": 1}
        left = {"a": 1}
        right = {"a": 1, "c": 3}
        merged, conflicts = three_way_merge_dicts(base, left, right)
        assert merged == {"a": 1, "c": 3}
        assert conflicts == []

    def test_both_add_different_keys(self) -> None:
        base: dict[str, int] = {"a": 1}
        left = {"a": 1, "b": 2}
        right = {"a": 1, "c": 3}
        merged, conflicts = three_way_merge_dicts(base, left, right)
        assert merged == {"a": 1, "b": 2, "c": 3}
        assert conflicts == []

    def test_both_same_change_no_conflict(self) -> None:
        base = {"a": 1, "b": 2}
        left = {"a": 1, "b": 99}
        right = {"a": 1, "b": 99}
        merged, conflicts = three_way_merge_dicts(base, left, right)
        assert merged == {"a": 1, "b": 99}
        assert conflicts == []

    def test_both_delete_same_key(self) -> None:
        base = {"a": 1, "b": 2}
        left = {"a": 1}
        right = {"a": 1}
        merged, conflicts = three_way_merge_dicts(base, left, right)
        assert merged == {"a": 1}
        assert conflicts == []

    def test_conflict_fail_strategy(self) -> None:
        base = {"a": 1, "x": 10}
        left = {"a": 1, "x": 20}
        right = {"a": 1, "x": 30}
        merged, conflicts = three_way_merge_dicts(base, left, right, strategy="fail")
        assert conflicts == ["x"]
        # Base value kept for conflicting key
        assert merged["x"] == 10

    def test_conflict_source_wins_strategy(self) -> None:
        base = {"a": 1, "x": 10}
        left = {"a": 1, "x": 20}
        right = {"a": 1, "x": 30}
        merged, conflicts = three_way_merge_dicts(base, left, right, strategy="source-wins")
        assert conflicts == []
        assert merged["x"] == 20  # left (source) wins

    def test_mixed_adds_deletes_modifies(self) -> None:
        base = {"a": 1, "b": 2, "c": 3, "d": 4}
        left = {"a": 10, "b": 2, "d": 4, "e": 5}  # modify a, delete c, add e
        right = {"a": 1, "b": 20, "c": 3}  # modify b, delete d
        merged, conflicts = three_way_merge_dicts(base, left, right)
        assert merged == {"a": 10, "b": 20, "e": 5}  # a from left, b from right, c&d deleted
        assert conflicts == []

    def test_invalid_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown merge strategy"):
            three_way_merge_dicts({}, {}, {}, strategy="invalid")

    def test_left_delete_right_modify_conflict(self) -> None:
        base = {"x": 1}
        left: dict[str, int] = {}  # delete x
        right = {"x": 99}  # modify x
        merged, conflicts = three_way_merge_dicts(base, left, right, strategy="fail")
        assert conflicts == ["x"]

    def test_left_delete_right_modify_source_wins(self) -> None:
        base = {"x": 1}
        left: dict[str, int] = {}  # delete x
        right = {"x": 99}  # modify x
        merged, conflicts = three_way_merge_dicts(base, left, right, strategy="source-wins")
        assert conflicts == []
        assert "x" not in merged  # left (delete) wins

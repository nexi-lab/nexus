"""Generic three-way merge for dict-like structures (Issue #1273).

Pure functions, zero imports from nexus — only stdlib + typing.
Reusable by both ContextBranchService (file content) and namespace fork
(mount-table overlays).

Three-way merge algorithm:
    1. Compute changes from base→left (fork) and base→right (parent-current).
    2. Non-overlapping changes apply cleanly.
    3. Overlapping changes with same value are fine (convergent).
    4. Overlapping changes with different values are conflicts.
    5. Strategy 'fail' raises on conflict; 'source-wins' picks left.
"""

from typing import TypeVar

V = TypeVar("V")

# Change types returned by _compute_dict_changes
_ADD = "add"
_DELETE = "delete"
_MODIFY = "modify"


def _compute_dict_changes(
    base: dict[str, V],
    target: dict[str, V],
) -> dict[str, tuple[str, V | None]]:
    """Compute the set of changes from *base* to *target*.

    Returns a dict mapping each changed key to a ``(change_type, value)``
    tuple where *change_type* is one of ``"add"``, ``"delete"``, or
    ``"modify"`` and *value* is the new value (``None`` for deletes).

    Pure function — does not mutate either argument.

    Args:
        base: The original dict (snapshot at fork time).
        target: The current dict (fork or parent-current).

    Returns:
        Dict of ``{key: (change_type, new_value | None)}``.
    """
    changes: dict[str, tuple[str, V | None]] = {}

    # Detect additions and modifications
    for key, value in target.items():
        if key not in base:
            changes[key] = (_ADD, value)
        elif base[key] != value:
            changes[key] = (_MODIFY, value)

    # Detect deletions
    for key in base:
        if key not in target:
            changes[key] = (_DELETE, None)

    return changes


def three_way_merge_dicts(
    base: dict[str, V],
    left: dict[str, V],
    right: dict[str, V],
    *,
    strategy: str = "fail",
) -> tuple[dict[str, V], list[str]]:
    """Three-way merge of two dicts against a common base.

    Computes independent change sets (base→left, base→right) and applies
    non-conflicting changes to produce a merged result.

    Conflict handling depends on *strategy*:
        ``"fail"``: Return conflicts list; caller decides how to proceed.
        ``"source-wins"``: Left (fork/source) wins on conflict.

    Args:
        base: Common ancestor dict (snapshot at fork time).
        left: Left branch dict (fork's current state).
        right: Right branch dict (parent's current state).
        strategy: ``"fail"`` or ``"source-wins"``.

    Returns:
        Tuple of ``(merged_dict, conflict_keys)``.
        *merged_dict* contains all non-conflicting changes applied.
        *conflict_keys* is empty when strategy is ``"source-wins"``.

    Raises:
        ValueError: If *strategy* is not ``"fail"`` or ``"source-wins"``.
    """
    if strategy not in ("fail", "source-wins"):
        msg = f"Unknown merge strategy: {strategy!r}"
        raise ValueError(msg)

    left_changes = _compute_dict_changes(base, left)
    right_changes = _compute_dict_changes(base, right)

    # Start from base, apply all changes
    merged = dict(base)
    conflict_keys: list[str] = []

    # Keys changed only on one side — apply cleanly
    left_only = set(left_changes) - set(right_changes)
    right_only = set(right_changes) - set(left_changes)

    for key in left_only:
        change_type, value = left_changes[key]
        if change_type == _DELETE:
            merged.pop(key, None)
        elif value is not None:
            merged[key] = value

    for key in right_only:
        change_type, value = right_changes[key]
        if change_type == _DELETE:
            merged.pop(key, None)
        elif value is not None:
            merged[key] = value

    # Keys changed on both sides — check for conflicts
    both = set(left_changes) & set(right_changes)
    for key in sorted(both):
        left_ct, left_val = left_changes[key]
        right_ct, right_val = right_changes[key]

        # Convergent: same change type and same value → no conflict
        if left_ct == right_ct and left_val == right_val:
            if left_ct == _DELETE:
                merged.pop(key, None)
            elif left_val is not None:
                merged[key] = left_val
            continue

        # Divergent: actual conflict
        if strategy == "source-wins":
            # Left (fork/source) wins
            if left_ct == _DELETE:
                merged.pop(key, None)
            elif left_val is not None:
                merged[key] = left_val
        else:
            # strategy == "fail": record conflict, keep base value
            conflict_keys.append(key)

    return merged, conflict_keys

"""Grant derivation pure functions (Issue #1271).

Core logic for computing child grants from parent grants.
All functions are pure (no side effects, no I/O) and can be
tested with property-based testing (Hypothesis).

The anti-escalation invariant holds for all modes:
    derived_grants is a subset of parent_grants (by object_id)

Modes:
    COPY:   parent grants → filter scope_prefix → remove removed_grants
            → downgrade readonly_paths to viewer → cap at MAX
    CLEAN:  empty → add only add_grants that exist in parent → cap at MAX
    SHARED: all parent grants (within scope_prefix) → cap at MAX
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus.services.delegation.errors import (
    EscalationError,
    InvalidDelegationModeError,
    TooManyGrantsError,
)
from nexus.services.delegation.models import DelegationMode

MAX_DELEGATABLE_GRANTS = 1000


@dataclass(frozen=True)
class GrantSpec:
    """A single grant to materialize as a ReBAC tuple.

    Attributes:
        object_type: Entity type (e.g. "file").
        object_id: Entity identifier (e.g. "/workspace/proj/data.csv").
        relation: ReBAC relation (e.g. "direct_viewer" or "direct_editor").
    """

    object_type: str
    object_id: str
    relation: str


def derive_grants(
    parent_grants: list[tuple[str, str]],
    mode: DelegationMode,
    remove_grants: list[str] | None = None,
    add_grants: list[str] | None = None,
    readonly_paths: list[str] | None = None,
    scope_prefix: str | None = None,
) -> list[GrantSpec]:
    """Derive child grants from parent grants according to delegation mode.

    This is the core anti-escalation function. For all modes, the returned
    grants are a subset of what the parent has access to.

    Args:
        parent_grants: Parent's grants as (relation, object_id) tuples.
            relation is e.g. "direct_editor" or "direct_viewer".
        mode: Delegation mode (COPY, CLEAN, or SHARED).
        remove_grants: Paths to exclude (COPY mode only).
        add_grants: Paths to include (CLEAN mode only, must be subset of parent).
        readonly_paths: Paths to downgrade from editor to viewer (COPY mode).
        scope_prefix: Optional path prefix filter. Only grants whose object_id
            starts with this prefix are included.

    Returns:
        List of GrantSpec objects for the child agent.

    Raises:
        EscalationError: If add_grants contains paths not in parent grants.
        TooManyGrantsError: If derived grants exceed MAX_DELEGATABLE_GRANTS.
        InvalidDelegationModeError: If mode is not a valid DelegationMode.
    """
    remove_set = frozenset(remove_grants) if remove_grants else frozenset()
    add_set = frozenset(add_grants) if add_grants else frozenset()
    readonly_set = frozenset(readonly_paths) if readonly_paths else frozenset()

    # Build lookup of parent grants: object_id -> relation
    parent_map: dict[str, str] = {}
    for relation, object_id in parent_grants:
        # Keep the highest-privilege relation if duplicates exist
        existing = parent_map.get(object_id)
        if existing is None or _relation_rank(relation) > _relation_rank(existing):
            parent_map[object_id] = relation

    if mode is DelegationMode.COPY:
        result = _derive_copy(parent_map, remove_set, readonly_set, scope_prefix)
    elif mode is DelegationMode.CLEAN:
        result = _derive_clean(parent_map, add_set, scope_prefix)
    elif mode is DelegationMode.SHARED:
        result = _derive_shared(parent_map, scope_prefix)
    else:
        raise InvalidDelegationModeError(f"Unknown delegation mode: {mode}")

    if len(result) > MAX_DELEGATABLE_GRANTS:
        raise TooManyGrantsError(
            f"Derived {len(result)} grants, exceeds maximum of {MAX_DELEGATABLE_GRANTS}"
        )

    return result


def _relation_rank(relation: str) -> int:
    """Rank relations by privilege level (higher = more privilege)."""
    if "editor" in relation:
        return 2
    if "viewer" in relation:
        return 1
    return 0


def _matches_prefix(object_id: str, prefix: str | None) -> bool:
    """Check if object_id matches the scope prefix."""
    if prefix is None:
        return True
    # Normalize: ensure prefix ends with / for directory matching
    normalized = prefix.rstrip("/") + "/"
    return object_id.startswith(normalized) or object_id == prefix.rstrip("/")


def _derive_copy(
    parent_map: dict[str, str],
    remove_set: frozenset[str],
    readonly_set: frozenset[str],
    scope_prefix: str | None,
) -> list[GrantSpec]:
    """COPY mode: start with parent grants, narrow down."""
    result: list[GrantSpec] = []
    for object_id, relation in sorted(parent_map.items()):
        # Filter by scope prefix
        if not _matches_prefix(object_id, scope_prefix):
            continue
        # Remove explicitly excluded paths
        if object_id in remove_set:
            continue
        # Downgrade readonly paths from editor to viewer
        if object_id in readonly_set and "editor" in relation:
            relation = relation.replace("editor", "viewer")
        result.append(GrantSpec(object_type="file", object_id=object_id, relation=relation))
    return result


def _derive_clean(
    parent_map: dict[str, str],
    add_set: frozenset[str],
    scope_prefix: str | None,
) -> list[GrantSpec]:
    """CLEAN mode: empty set, add only specified grants from parent."""
    result: list[GrantSpec] = []
    # Validate: all add_grants must exist in parent
    parent_ids = frozenset(parent_map.keys())
    escalation = add_set - parent_ids
    if escalation:
        raise EscalationError(f"Cannot add grants not held by parent: {sorted(escalation)}")
    for object_id in sorted(add_set):
        if not _matches_prefix(object_id, scope_prefix):
            continue
        relation = parent_map[object_id]
        result.append(GrantSpec(object_type="file", object_id=object_id, relation=relation))
    return result


def _derive_shared(
    parent_map: dict[str, str],
    scope_prefix: str | None,
) -> list[GrantSpec]:
    """SHARED mode: return all parent grants (within scope_prefix)."""
    result: list[GrantSpec] = []
    for object_id, relation in sorted(parent_map.items()):
        if not _matches_prefix(object_id, scope_prefix):
            continue
        result.append(GrantSpec(object_type="file", object_id=object_id, relation=relation))
    return result

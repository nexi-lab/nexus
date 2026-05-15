"""Permission filter chain for Nexus filter_list() (Issue #899).

Decomposes the ~450-line filter_list() into a chain of composable strategies:
1. TigerBitmapStrategy  — O(1) bitmap filtering via Tiger Cache
2. LeopardIndexStrategy  — cached accessible directory index
3. HierarchyPreFilterStrategy — batch ancestor checks via rebac_check_bulk()
4. ZonePreFilterStrategy  — cross-zone path elimination
5. BulkReBACStrategy  — final fallback via rebac_check_bulk()

Each strategy receives remaining paths and returns (allowed, remaining).
The chain short-circuits once all paths are resolved.
"""

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from nexus.core.path_utils import unscope_internal_path

if TYPE_CHECKING:
    from nexus.bricks.rebac.manager import ReBACManager
    from nexus.bricks.rebac.permission_cache import PermissionCacheCoordinator
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterContext:
    """Immutable context passed through the filter chain."""

    paths: list[str]
    subject: tuple[str, str]
    zone_id: str
    context: "OperationContext"
    cache: "PermissionCacheCoordinator"
    rebac_manager: "ReBACManager"
    dlc: Any = None


@dataclass
class FilterResult:
    """Result from a filter strategy."""

    allowed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    short_circuit: bool = False


class FilterStrategy(Protocol):
    """Single step in the permission filter chain."""

    def apply(self, ctx: FilterContext, remaining: list[str]) -> FilterResult: ...


# =============================================================================
# Strategy 1: Tiger Bitmap (O(1) bitmap)
# =============================================================================


class TigerBitmapStrategy:
    """Try O(1) bitmap filtering via Tiger Cache."""

    def apply(self, ctx: FilterContext, remaining: list[str]) -> FilterResult:
        result = ctx.cache.try_bitmap_filter(remaining, ctx.subject, ctx.zone_id)
        if result is None:
            return FilterResult(allowed=[], remaining=remaining)

        allowed, still_remaining = result
        logger.debug(f"[TIGER-BITMAP] {len(allowed)} allowed, {len(still_remaining)} remaining")

        # Check bitmap completeness — if complete, skip fallback.
        # Only short-circuit when the bitmap found SOME allowed paths.
        # If allowed is empty, the bitmap may be stale/incomplete (e.g.,
        # zone-prefixed vs non-prefixed path mismatch) — fall through to
        # the full ReBAC check to avoid false denials.
        if still_remaining and allowed and ctx.cache.is_bitmap_complete(ctx.subject, ctx.zone_id):
            logger.info(f"[BITMAP-COMPLETE] Skipped {len(still_remaining)} fallback checks")
            return FilterResult(allowed=allowed, remaining=[], short_circuit=True)

        return FilterResult(allowed=allowed, remaining=still_remaining)


# =============================================================================
# Strategy 2: Leopard Directory Index (cached dir grants)
# =============================================================================


class LeopardIndexStrategy:
    """Check cached accessible directories for path inheritance."""

    def apply(self, ctx: FilterContext, remaining: list[str]) -> FilterResult:
        if not remaining:
            return FilterResult()

        allowed, still_remaining = ctx.cache.try_leopard_lookup(remaining, ctx.subject, ctx.zone_id)
        return FilterResult(allowed=allowed, remaining=still_remaining)


# =============================================================================
# Strategy 3: Hierarchy Pre-Filter (batch ancestor checks)
# =============================================================================


class HierarchyPreFilterStrategy:
    """Batch-check parent directories to eliminate entire subtrees.

    Groups paths by parent directory, checks unique parents via
    rebac_check_bulk(), then only keeps paths under accessible parents.
    Uses FULL ancestor walk (not just immediate parent) for consistency
    with _check_rebac_batched() (Issue #899, #4A).
    """

    def apply(self, ctx: FilterContext, remaining: list[str]) -> FilterResult:
        if len(remaining) <= 100:
            # Not worth the overhead for small sets
            return FilterResult(allowed=[], remaining=remaining)

        # Group paths by their immediate parent directory
        paths_by_parent: dict[str, list[str]] = defaultdict(list)
        for p in remaining:
            parent = os.path.dirname(p) or "/"
            paths_by_parent[parent].append(p)

        unique_parents = list(paths_by_parent.keys())

        if len(unique_parents) >= len(remaining):
            # No dedup benefit — skip
            return FilterResult(allowed=[], remaining=remaining)

        subject = ctx.subject

        # Batch check unique parent directories
        parent_checks = [
            (subject, "read", ("file", unscope_internal_path(parent))) for parent in unique_parents
        ]
        parent_results = ctx.rebac_manager.rebac_check_bulk(parent_checks, zone_id=ctx.zone_id)

        accessible_parents = {
            parent
            for parent, check in zip(unique_parents, parent_checks, strict=False)
            if parent_results.get(check, False)
        }

        logger.info(
            f"[HIERARCHY-PREFILTER] {len(accessible_parents)}/{len(unique_parents)} "
            f"parents accessible"
        )

        # Store accessible directories in Leopard index
        if accessible_parents:
            ctx.cache.record_accessible_dirs(accessible_parents, subject, ctx.zone_id)

        # Only keep paths under accessible parents
        if len(accessible_parents) < len(unique_parents):
            kept: list[str] = []
            for parent in accessible_parents:
                kept.extend(paths_by_parent[parent])

            skipped = len(remaining) - len(kept)
            logger.info(
                f"[HIERARCHY-PREFILTER] Reduced fallback: {len(remaining)} -> "
                f"{len(kept)} paths (skipped {skipped} under denied parents)"
            )

            # Do NOT mark bitmap complete on empty results — parent grants
            # may exist but not resolve in bulk mode (e.g., viewer role).
            # if not accessible_parents and len(remaining) > 100:
            #     ctx.cache.mark_bitmap_complete(subject, ctx.zone_id)

            return FilterResult(allowed=[], remaining=kept)

        return FilterResult(allowed=[], remaining=remaining)


# =============================================================================
# Strategy 4: Zone Pre-Filter (cross-zone elimination)
# =============================================================================


class ZonePreFilterStrategy:
    """Skip paths belonging to other zones."""

    @staticmethod
    def _extract_zone_id(path: str) -> str | None:
        """Return the zone id from supported internal zone prefixes."""
        for prefix in ("/zone/", "/zones/"):
            if path.startswith(prefix):
                zone_id = path[len(prefix) :].split("/", 1)[0]
                return zone_id or None
        return None

    def apply(self, ctx: FilterContext, remaining: list[str]) -> FilterResult:
        if not remaining:
            return FilterResult()

        kept: list[str] = []
        skipped = 0
        for path in remaining:
            path_zone_id = self._extract_zone_id(path)
            if path_zone_id is not None and path_zone_id != ctx.zone_id:
                skipped += 1
                continue
            kept.append(path)

        if skipped:
            logger.debug(f"[ZONE-PREFILTER] Skipped {skipped} paths not in zone {ctx.zone_id}")

        return FilterResult(allowed=[], remaining=kept)


# =============================================================================
# Strategy 5: Bulk ReBAC (final fallback)
# =============================================================================


class BulkReBACStrategy:
    """Final fallback: check all remaining paths via rebac_check_bulk().

    Includes retry-once on failure (Issue #899, #7A).
    """

    def apply(self, ctx: FilterContext, remaining: list[str]) -> FilterResult:
        if not remaining:
            return FilterResult()

        subject = ctx.subject

        checks = []
        for path in remaining:
            checks.append((subject, "read", ("file", unscope_internal_path(path))))

        # Retry-once on transient I/O failures only
        try:
            results = ctx.rebac_manager.rebac_check_bulk(checks, zone_id=ctx.zone_id)
        except (OSError, ConnectionError, TimeoutError) as e:
            logger.warning(f"[BULK-REBAC] Bulk check failed, retrying once: {e}")
            try:
                results = ctx.rebac_manager.rebac_check_bulk(checks, zone_id=ctx.zone_id)
            except Exception as e2:  # fail-safe: second failure → deny all (fail-closed)
                logger.error(f"[BULK-REBAC] Bulk check failed twice: {e2}")
                return FilterResult(allowed=[], remaining=[], short_circuit=True)
        except Exception as e:  # fail-safe: non-retryable error → deny all (fail-closed)
            logger.error(f"[BULK-REBAC] Bulk check failed (non-retryable): {e}")
            return FilterResult(allowed=[], remaining=[], short_circuit=True)

        allowed = [
            path
            for path, check in zip(remaining, checks, strict=False)
            if results.get(check, False)
        ]

        # Only mark bitmap complete when bulk found SOME results from a large set.
        # An empty result may indicate parent-level grants that aren't resolved
        # in bulk mode — marking empty as "complete" would block fallback on
        # subsequent requests and deny all access incorrectly.
        if allowed and len(remaining) > 100:
            ctx.cache.mark_bitmap_complete(subject, ctx.zone_id)

        return FilterResult(allowed=allowed, remaining=[], short_circuit=True)


# =============================================================================
# Chain Composition
# =============================================================================

# Default strategy chain in priority order
DEFAULT_FILTER_CHAIN: list[FilterStrategy] = [
    TigerBitmapStrategy(),
    LeopardIndexStrategy(),
    HierarchyPreFilterStrategy(),
    ZonePreFilterStrategy(),
    BulkReBACStrategy(),
]


def run_filter_chain(
    ctx: FilterContext,
    chain: list[FilterStrategy] | None = None,
) -> list[str]:
    """Execute the filter strategy chain.

    Processes paths through each strategy in order. Each strategy can:
    - Move paths from remaining to allowed (confirmed accessible)
    - Reduce remaining (skip inaccessible subtrees)
    - Short-circuit to stop the chain early

    Args:
        ctx: Filter context with paths, subject, caches, etc.
        chain: Optional custom strategy chain (defaults to DEFAULT_FILTER_CHAIN)

    Returns:
        List of allowed paths.
    """
    strategies = chain if chain is not None else DEFAULT_FILTER_CHAIN
    allowed: list[str] = []
    remaining = list(ctx.paths)

    for strategy in strategies:
        if not remaining:
            break

        result = strategy.apply(ctx, remaining)
        allowed.extend(result.allowed)
        remaining = result.remaining

        if result.short_circuit:
            break

    return allowed

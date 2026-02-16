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

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.services.permissions.permission_cache import PermissionCacheCoordinator
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterContext:
    """Immutable context passed through the filter chain."""

    paths: list[str]
    subject: tuple[str, str]
    zone_id: str
    context: OperationContext
    cache: PermissionCacheCoordinator
    rebac_manager: EnhancedReBACManager
    router: Any = None


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

        # Check bitmap completeness — if complete, skip fallback
        if still_remaining and ctx.cache.is_bitmap_complete(ctx.subject, ctx.zone_id):
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

        # Multi-level hierarchy: if too many parents, check top-level first
        if len(unique_parents) > 200:
            unique_parents = self._top_level_prune(unique_parents, subject, ctx)
            if not unique_parents:
                # All top-level dirs denied
                ctx.cache.mark_bitmap_complete(subject, ctx.zone_id)
                return FilterResult(allowed=[], remaining=[], short_circuit=True)

        # Batch check unique parent directories
        parent_checks = [(subject, "read", ("file", parent)) for parent in unique_parents]
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

            if not accessible_parents and len(remaining) > 100:
                ctx.cache.mark_bitmap_complete(subject, ctx.zone_id)

            return FilterResult(allowed=[], remaining=kept)

        return FilterResult(allowed=[], remaining=remaining)

    def _top_level_prune(
        self,
        unique_parents: list[str],
        subject: tuple[str, str],
        ctx: FilterContext,
    ) -> list[str]:
        """Check top-level dirs first to quickly eliminate large subtrees."""
        top_level_dirs: set[str] = set()
        for parent in unique_parents:
            parts = parent.strip("/").split("/")
            if parts and parts[0]:
                top_level_dirs.add("/" + parts[0])

        top_level_checks = [(subject, "read", ("file", d)) for d in top_level_dirs]
        top_level_results = ctx.rebac_manager.rebac_check_bulk(
            top_level_checks, zone_id=ctx.zone_id
        )

        denied_top_level = {
            d
            for d, check in zip(top_level_dirs, top_level_checks, strict=False)
            if not top_level_results.get(check, False)
        }

        if denied_top_level:
            filtered_parents = []
            for parent in unique_parents:
                top = "/" + parent.strip("/").split("/")[0] if parent != "/" else "/"
                if top not in denied_top_level:
                    filtered_parents.append(parent)

            logger.info(
                f"[HIERARCHY-TOPLEVEL] {len(denied_top_level)}/{len(top_level_dirs)} "
                f"top-level dirs denied, reduced parents: "
                f"{len(unique_parents)} -> {len(filtered_parents)}"
            )
            return filtered_parents

        return unique_parents


# =============================================================================
# Strategy 4: Zone Pre-Filter (cross-zone elimination)
# =============================================================================


class ZonePreFilterStrategy:
    """Skip paths belonging to other zones."""

    def apply(self, ctx: FilterContext, remaining: list[str]) -> FilterResult:
        if not remaining:
            return FilterResult()

        kept: list[str] = []
        skipped = 0
        for path in remaining:
            if path.startswith("/zones/"):
                path_parts = path.split("/")
                if len(path_parts) >= 3 and path_parts[2] != ctx.zone_id:
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
            obj_type = "file"
            if ctx.router and not path.startswith("/workspace"):
                try:
                    route = ctx.router.route(
                        path,
                        zone_id=ctx.context.zone_id,
                        is_admin=ctx.context.is_admin,
                    )
                    if hasattr(route, "namespace") and route.namespace:
                        obj_type = route.namespace
                except Exception:
                    pass
            checks.append((subject, "read", (obj_type, path)))

        # Retry-once on transient I/O failures only
        try:
            results = ctx.rebac_manager.rebac_check_bulk(checks, zone_id=ctx.zone_id)
        except (OSError, ConnectionError, TimeoutError) as e:
            logger.warning(f"[BULK-REBAC] Bulk check failed, retrying once: {e}")
            try:
                results = ctx.rebac_manager.rebac_check_bulk(checks, zone_id=ctx.zone_id)
            except Exception as e2:
                logger.error(f"[BULK-REBAC] Bulk check failed twice: {e2}")
                return FilterResult(allowed=[], remaining=[], short_circuit=True)
        except Exception as e:
            logger.error(f"[BULK-REBAC] Bulk check failed (non-retryable): {e}")
            return FilterResult(allowed=[], remaining=[], short_circuit=True)

        allowed = [
            path
            for path, check in zip(remaining, checks, strict=False)
            if results.get(check, False)
        ]

        # If bulk found 0 additional paths from a large set, mark bitmap complete
        if not allowed and len(remaining) > 100:
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

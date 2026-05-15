"""ReBAC search-result filtering helpers.

Shared by the HTTP search router and MCP search tools so both surfaces
apply identical file-level permission filtering (#3731).

Extracted from ``nexus.server.api.v2.routers.search`` to live in the
``nexus.lib`` tier, which bricks are allowed to import.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# When a permission enforcer is active we over-fetch to compensate for
# results that will be stripped during ReBAC filtering.  3x is the legacy
# value chosen empirically when #2056 landed.
REBAC_OVERFETCH_FACTOR: int = 3

# Threshold above which a high denial rate triggers a server-side warning.
REBAC_HIGH_DENIAL_WARN_THRESHOLD: float = 0.5


def normalize_path(path: str) -> str:
    """Ensure path is absolute for ReBAC filter_list compatibility."""
    if not path.startswith("/"):
        return f"/{path}"
    return path


def apply_rebac_filter(
    results: list[Any],
    permission_enforcer: Any | None,
    auth_result: dict[str, Any],
    zone_id: str,
    path_extractor: Any | None = None,
) -> tuple[list[Any], float]:
    """Apply ReBAC file-level permission filtering to search results.

    Returns (filtered_results, filter_time_ms).

    Args:
        results: Search results to filter.
        permission_enforcer: PermissionEnforcer instance (or None to skip).
        auth_result: Authentication dict with subject_id, is_admin, etc.
        zone_id: Zone ID for ReBAC scope.
        path_extractor: Callable ``(result) -> str`` that extracts the file
            path from a result element.  Defaults to ``lambda r: r.path``.
    """
    if permission_enforcer is None:
        return results, 0.0

    if not hasattr(permission_enforcer, "filter_search_results"):
        return results, 0.0

    if path_extractor is None:
        path_extractor = lambda r: r.path  # noqa: E731

    user_id = auth_result.get("subject_id") or auth_result.get("user_id", "anonymous")
    is_admin = bool(auth_result.get("is_admin", False))

    # Two-pass: deduplicate paths for the permission check, then filter
    # the original ordered list against the permitted set.
    unique_abs_paths: list[str] = []
    seen: set[str] = set()
    result_abs_paths: list[str] = []
    for r in results:
        abs_path = normalize_path(path_extractor(r))
        result_abs_paths.append(abs_path)
        if abs_path not in seen:
            seen.add(abs_path)
            unique_abs_paths.append(abs_path)

    filter_start = time.perf_counter()
    permitted_abs = permission_enforcer.filter_search_results(
        unique_abs_paths,
        user_id=user_id,
        zone_id=zone_id,
        is_admin=is_admin,
    )
    filter_ms = (time.perf_counter() - filter_start) * 1000

    logger.debug(
        "[SEARCH-REBAC] permitted %d/%d paths in %.1fms",
        len(permitted_abs),
        len(unique_abs_paths),
        filter_ms,
    )

    permitted_set = set(permitted_abs)
    filtered = [r for r, p in zip(results, result_abs_paths, strict=True) if p in permitted_set]
    return filtered, filter_ms


def compute_rebac_fetch_limit(effective_limit: int, has_enforcer: bool) -> int:
    """Compute the over-fetch size for a given effective limit."""
    if not has_enforcer:
        return effective_limit
    return effective_limit * REBAC_OVERFETCH_FACTOR


def rebac_denial_stats(
    pre_filter_count: int, post_filter_count: int, effective_limit: int
) -> dict[str, Any]:
    """Compute denial-rate instrumentation for response envelopes."""
    denial_rate = 0.0 if pre_filter_count == 0 else 1.0 - (post_filter_count / pre_filter_count)

    truncated = (
        post_filter_count < effective_limit and denial_rate >= REBAC_HIGH_DENIAL_WARN_THRESHOLD
    )
    if truncated:
        logger.warning(
            "[SEARCH-REBAC] high denial rate (%.1f%%) caused undercount: "
            "got %d of %d requested; consider paginating or increasing limit",
            denial_rate * 100.0,
            post_filter_count,
            effective_limit,
        )
    return {
        "permission_denial_rate": round(denial_rate, 4),
        "truncated_by_permissions": truncated,
    }

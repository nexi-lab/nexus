"""Rust-guarded prefix helpers for descendant-access hot paths (Issue #3951).

`batch_paths_under_prefixes` calls the Rust kernel helper first (the sort
amortizes across many prefixes). `any_path_under_prefix` deliberately uses
a Python linear scan with early-exit: the Rust `any_path_starts_with`
sorts the entire path list internally for every call (O(N log N)), which
is slower than `any(startswith)` with early termination on the typical
hot-path pattern (one matching descendant near the front of the list).
"""

from __future__ import annotations

from nexus._rust_compat import batch_prefix_check as _rust_batch


def any_path_under_prefix(paths: "list[str] | set[str]", prefix: str) -> bool:
    """Return True if any path equals prefix or is a descendant of it.

    Safe for trailing-slash variation: "/a/b/" and "/a/b" both match
    descendants like "/a/b/c".

    Implementation: Python linear scan with early exit on first match.
    The Rust `any_path_starts_with` sorts internally (O(N log N)) which
    is wasteful for single-prefix repeated calls (e.g.,
    compute_from_tiger_bitmap iterating directories) where the typical
    case finds a match in the first few paths.
    """
    exact = prefix.rstrip("/")
    if not exact:  # root prefix matches everything non-empty
        return any(True for _ in paths)
    norm = exact + "/"
    return any(p == exact or p.startswith(norm) for p in paths)


def batch_paths_under_prefixes(
    paths: "list[str] | set[str]",
    prefixes: list[str],
) -> list[bool]:
    """For each prefix, return True if any path equals it or is a descendant.

    Result order matches the order of *prefixes*.
    """
    paths_list: list[str] = list(paths) if isinstance(paths, set) else paths
    if _rust_batch is not None:
        return list(_rust_batch(paths_list, prefixes))
    results: list[bool] = []
    for pfx in prefixes:
        exact = pfx.rstrip("/")
        norm = exact + "/"
        results.append(any(p == exact or p.startswith(norm) for p in paths_list))
    return results

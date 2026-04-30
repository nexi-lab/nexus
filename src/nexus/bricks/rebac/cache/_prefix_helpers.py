"""Rust-guarded prefix helpers for descendant-access hot paths (Issue #3951).

Both functions try the kernel primitive first; if unavailable they fall back
to pure-Python with identical semantics.
"""

from __future__ import annotations

from nexus._rust_compat import any_path_starts_with as _rust_any
from nexus._rust_compat import batch_prefix_check as _rust_batch


def any_path_under_prefix(paths: "list[str] | set[str]", prefix: str) -> bool:
    """Return True if any path equals prefix or is a descendant of it.

    Safe for trailing-slash variation: "/a/b/" and "/a/b" both match
    descendants like "/a/b/c".
    """
    paths_list: list[str] = list(paths) if isinstance(paths, set) else paths
    if _rust_any is not None:
        return bool(_rust_any(paths_list, prefix))
    exact = prefix.rstrip("/")
    norm = exact + "/"
    return any(p == exact or p.startswith(norm) for p in paths_list)


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

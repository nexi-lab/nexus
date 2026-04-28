"""Module-level helpers for kernel-direct nexus-fs callers.

These replace the ``SlimNexusFS`` facade methods that did not justify
their own wrapper class. Callers hold a ``NexusFS`` directly and pass
``LOCAL_CONTEXT`` to its ``sys_*`` methods, falling back here only for
the few operations that need Python-side orchestration (mounts.json
scrub, multi-step shutdown, grep/glob loops).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


LOCAL_CONTEXT = OperationContext(
    user_id="local",
    groups=[],
    zone_id=ROOT_ZONE_ID,
    is_admin=True,
)


_TRIGRAM_LAZY_BUILD_THRESHOLD = 500
_TRIGRAM_MAX_FILE_SIZE = 1024 * 1024
_TRIGRAM_BUILD_LOCK = threading.Lock()
_TRIGRAM_BUILDS_IN_PROGRESS: set[str] = set()


def list_mounts(kernel: NexusFS) -> list[str]:
    """Return the sorted list of mount-point paths registered in *kernel*."""
    py_kernel = getattr(kernel, "_kernel", None)
    if py_kernel is None:
        return []
    from nexus.core.path_utils import extract_zone_id

    return sorted(extract_zone_id(c)[1] for c in py_kernel.get_mount_points())


def unmount(kernel: NexusFS, mount_point: str) -> None:
    """Remove *mount_point* and clean up runtime + persisted state.

    The runtime tear-down (metastore delete + dcache evict + routing
    remove) is a single ``kernel.sys_unlink`` call — sys_unlink delegates
    to ``dlc::unmount`` when the entry is a DT_MOUNT. Only the
    ``mounts.json`` scrub stays Python-side because the kernel doesn't
    own that config file.
    """
    from nexus.core.path_utils import validate_path

    normalized = validate_path(mount_point, allow_root=False)
    meta = kernel.metadata.get(normalized)
    if meta is None or not meta.is_mount:
        raise ValueError(f"'{normalized}' is not a mount point")

    kernel.sys_unlink(normalized, context=LOCAL_CONTEXT)

    with contextlib.suppress(OSError):
        from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts
        from nexus.fs._uri import derive_mount_point, parse_uri

        existing = load_persisted_mounts()
        filtered = []
        for entry in existing:
            try:
                spec = parse_uri(entry["uri"])
                mp = derive_mount_point(spec, at=entry.get("at"))
                if mp != normalized:
                    filtered.append(entry)
            except Exception:
                filtered.append(entry)
        if len(filtered) != len(existing):
            save_persisted_mounts(filtered, merge=False)


def close(kernel: NexusFS) -> None:
    """Close the kernel and its metastore. Safe to call repeatedly."""
    try:
        _close = getattr(kernel, "close", None)
        if _close is not None:
            _close()
    finally:
        with contextlib.suppress(Exception):
            kernel.metadata.close()


def _trigram_index_path(zone_id: str) -> str:
    index_dir = os.path.join(os.path.expanduser("~"), ".nexus", "indexes")
    return os.path.join(index_dir, f"{os.path.basename(zone_id)}.trgm")


def _maybe_build_trigram_background(
    kernel: NexusFS, file_paths: list[str], index_path: str
) -> None:
    with _TRIGRAM_BUILD_LOCK:
        if index_path in _TRIGRAM_BUILDS_IN_PROGRESS:
            return
        _TRIGRAM_BUILDS_IN_PROGRESS.add(index_path)

    def _build() -> None:
        try:
            from nexus_runtime import build_trigram_index_from_entries

            entries: list[tuple[str, bytes]] = []
            for fp in file_paths:
                try:
                    content = kernel.sys_read(fp, context=LOCAL_CONTEXT)
                    if isinstance(content, bytes) and len(content) <= _TRIGRAM_MAX_FILE_SIZE:
                        entries.append((fp, content))
                except Exception:
                    continue

            if entries:
                os.makedirs(os.path.dirname(index_path), exist_ok=True)
                build_trigram_index_from_entries(entries, index_path)
                logger.debug(
                    "Issue #3711: Built trigram index at %s (%d files)", index_path, len(entries)
                )
        except Exception:
            logger.debug("Background trigram build failed", exc_info=True)
        finally:
            with _TRIGRAM_BUILD_LOCK:
                _TRIGRAM_BUILDS_IN_PROGRESS.discard(index_path)

    threading.Thread(target=_build, daemon=True).start()


def _ensure_trigram_index(kernel: NexusFS, file_paths: list[str], zone_id: str) -> str | None:
    """Return existing trigram index path, or kick off a background build."""
    index_path = _trigram_index_path(zone_id)
    if os.path.isfile(index_path):
        return index_path
    if len(file_paths) < _TRIGRAM_LAZY_BUILD_THRESHOLD:
        return None
    _maybe_build_trigram_background(kernel, file_paths, index_path)
    return None


def _trigram_candidates(
    index_path: str, pattern: str, path: str, ignore_case: bool
) -> list[str] | None:
    try:
        from nexus_runtime import trigram_search_candidates
    except (ImportError, OSError):
        return None
    try:
        candidates = trigram_search_candidates(index_path, pattern, ignore_case)
    except (OSError, ValueError, RuntimeError):
        return None
    if candidates is None:
        return None
    if path != "/":
        prefix = path if path.endswith("/") else path + "/"
        candidates = [c for c in candidates if c.startswith(prefix) or c == path]
    return candidates


def grep(
    kernel: NexusFS,
    pattern: str,
    path: str = "/",
    *,
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]]:
    """Search file contents for *pattern* via the Rust ``sys_grep`` syscall.

    Phase 6 / Phase 7: the entire batched-trigram-fallback flow this
    helper used to host (~80 lines) is now a single Rust syscall.
    `kernel.sys_grep` walks the metastore-recursive listing, reads each
    regular file, and runs `lib::search::search_lines` against the
    pattern.  Returns a list of `{file, line, content, match}` dicts —
    same shape as before so existing callers keep working unchanged.
    """
    inner: Any = getattr(kernel, "_kernel", kernel)
    return list(
        inner.sys_grep(
            pattern,
            path,
            ignore_case,
            max_results,
            LOCAL_CONTEXT.zone_id or ROOT_ZONE_ID,
        )
    )


def glob(kernel: NexusFS, pattern: str, path: str = "/") -> list[str]:
    """Find files matching *pattern* via the Rust ``sys_glob`` syscall.

    Phase 6 / Phase 7: replaces the Python ``glob_match_bulk`` +
    ``fnmatch`` fallback with a single ``kernel.sys_glob`` call —
    metastore-recursive listing + ``lib::glob::glob_match`` happen in
    pure Rust.
    """
    inner: Any = getattr(kernel, "_kernel", kernel)
    return list(inner.sys_glob(pattern, path, LOCAL_CONTEXT.zone_id or ROOT_ZONE_ID))


def _LEGACY_grep(
    kernel: NexusFS,
    pattern: str,
    path: str = "/",
    *,
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]]:
    """Legacy Python-side grep — retained as a fallback only."""
    import re

    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {exc}") from exc

    entries = kernel.sys_readdir(path, recursive=True, details=True, context=LOCAL_CONTEXT)
    all_files = [
        e["path"] for e in entries if isinstance(e, dict) and not e.get("is_directory", False)
    ]

    matches: list[dict[str, Any]] = []
    for fp in all_files:
        if len(matches) >= max_results:
            break
        try:
            content = kernel.sys_read(fp, context=LOCAL_CONTEXT)
        except Exception:
            continue
        if not isinstance(content, bytes):
            # DT_STREAM returns {data, next_offset} — grep only scans
            # regular files, so skip non-bytes payloads.
            continue
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            m = compiled.search(line)
            if m:
                matches.append({"file": fp, "line": line_no, "content": line, "match": m.group(0)})
                if len(matches) >= max_results:
                    return matches
    return matches


def _LEGACY_glob(kernel: NexusFS, pattern: str, path: str = "/") -> list[str]:
    """Legacy Python-side glob — retained as a fallback only."""
    entries = kernel.sys_readdir(path, recursive=True, details=False, context=LOCAL_CONTEXT)
    all_paths = [e for e in entries if isinstance(e, str)]
    if not all_paths:
        return []

    import fnmatch

    return [p for p in all_paths if fnmatch.fnmatch(p, pattern)]

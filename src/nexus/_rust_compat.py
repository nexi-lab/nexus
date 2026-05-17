"""Pure-Python compatibility shim (post-Rust-deletion).

nexus_runtime has been deleted — the kernel now runs as a separate
process accessed via gRPC (nexus.remote.kernel_client).  This module
provides the same export surface so existing imports continue to work
with pure-Python fallback implementations or None sentinels.
"""

from __future__ import annotations

import hashlib
import mmap
import os
import posixpath
import re
from typing import Any

# ---------------------------------------------------------------------------
# Feature flags — all False (no in-process Rust)
# ---------------------------------------------------------------------------
RUST_AVAILABLE: bool = False
RUST_EXTENSION_INSTALLED: bool = False
RUST_HASH_AVAILABLE: bool = False

# ---------------------------------------------------------------------------
# Kernel class — None (replaced by KernelClient over gRPC)
# ---------------------------------------------------------------------------
PyKernel = None

# ---------------------------------------------------------------------------
# BlobPackEngine — None (requires native binary)
# ---------------------------------------------------------------------------
BlobPackEngine: type[Any] | None = None

# ---------------------------------------------------------------------------
# Path utilities — pure Python implementations
# ---------------------------------------------------------------------------

_MULTI_SLASH = re.compile(r"/+")


def normalize_path(path: str) -> str:
    """Normalize an absolute virtual path (collapse //, resolve . / ..)."""
    if not path.startswith("/"):
        raise ValueError(f"Path must be absolute: {path}")
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        raise ValueError(f"Path traversal detected: {path}")
    return normalized


def split_path(path: str) -> list[str]:
    """Split a virtual path into components."""
    if not path or path == "/":
        return []
    return path.strip("/").split("/")


def get_parent(path: str) -> str | None:
    """Get parent directory path, or None for root."""
    parts = split_path(path)
    if not parts:
        return None
    if len(parts) < 2:
        return "/"
    return "/" + "/".join(parts[:-1])


def get_ancestors(path: str) -> list[str]:
    """Get all ancestor paths from most specific to least (excludes root)."""
    parts = split_path(path)
    if not parts:
        return []
    return ["/" + "/".join(parts[:i]) for i in range(len(parts), 0, -1)]


def get_parent_chain(path: str) -> list[tuple[str, str]]:
    """Get (child, parent) tuples for the full hierarchy."""
    parts = split_path(path)
    if len(parts) < 2:
        return []
    return [
        ("/" + "/".join(parts[:i]), "/" + "/".join(parts[: i - 1]))
        for i in range(len(parts), 1, -1)
    ]


def parent_path(path: str) -> str | None:
    """Return parent directory of path, or None for root."""
    if path == "/":
        return None
    path = path.rstrip("/")
    last_slash = path.rfind("/")
    if last_slash == 0:
        return "/"
    return path[:last_slash] if last_slash > 0 else None


def canonicalize_path(path: str, zone_id: str = "root") -> str:
    """Canonicalize a virtual path with zone prefix."""
    stripped = path.lstrip("/")
    return f"/{zone_id}/{stripped}" if stripped else f"/{zone_id}"


def extract_zone_id(canonical_path: str) -> tuple[str, str]:
    """Extract (zone_id, relative_path) from a canonical path."""
    parts = canonical_path.lstrip("/").split("/", 1)
    zone_id = parts[0]
    relative = "/" + parts[1] if len(parts) > 1 else "/"
    return zone_id, relative


def validate_path(path: str, allow_root: bool = False) -> str:
    """Validate and normalize a virtual path with security checks."""
    from nexus.contracts.exceptions import InvalidPathError

    original_path = path
    path = path.strip() if isinstance(path, str) else path

    if not path:
        raise InvalidPathError(original_path, "Path cannot be empty or whitespace-only")

    if path == "/" and not allow_root:
        raise InvalidPathError(
            "/",
            "Root path '/' not allowed for file operations. Use list('/') for directory listings.",
        )

    if not path.startswith("/"):
        path = "/" + path

    path = _MULTI_SLASH.sub("/", path)

    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    _INVALID_CHARS = ("\0", "\n", "\r", "\t")
    for char in _INVALID_CHARS:
        if char in path:
            raise InvalidPathError(path, f"Path contains invalid character: {repr(char)}")

    parts = path.split("/")
    for part in parts:
        if part and part != part.strip():
            raise InvalidPathError(
                path,
                f"Path component '{part}' has leading/trailing whitespace. "
                f"Path components must not contain spaces at start/end.",
            )

    if any(part in {".", ".."} for part in parts):
        raise InvalidPathError(path, "Path contains '.' or '..' segments")

    return path


def path_matches_pattern(path: str, pattern: str) -> bool:
    """Check if path matches a glob pattern (*, **, ?)."""
    if "*" not in pattern and "?" not in pattern:
        return path == pattern
    # Build regex from glob pattern
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern[i : i + 2] == "**":
            regex += ".*"
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                regex += "/?"
                i += 1
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            regex += "."
            i += 1
        elif pattern[i] in r"\.[]{}()+^$|":
            regex += "\\" + pattern[i]
            i += 1
        else:
            regex += pattern[i]
            i += 1
    try:
        compiled = re.compile("^" + regex + "$")
    except re.error:
        return False
    return bool(compiled.match(path))


def unscope_internal_path(path: str) -> str:
    """Strip internal zone/tenant/user prefix from a storage path."""
    parts = path.lstrip("/").split("/")
    skip = 0
    if parts and parts[0].startswith("tenant:"):
        skip = 1
        if len(parts) > 1 and parts[1].startswith("user:"):
            skip = 2
    elif parts and parts[0] == "zone" and len(parts) >= 2:
        skip = 2
        if len(parts) > 2 and parts[2].startswith("user:"):
            skip = 3
    if skip == 0:
        return path if path else "/"
    remaining = "/".join(parts[skip:])
    return f"/{remaining}" if remaining else "/"


# ---------------------------------------------------------------------------
# Hash functions — Python blake3 with hashlib SHA-256 fallback
# ---------------------------------------------------------------------------

_BLAKE3_AVAILABLE = False
_blake3_mod: Any = None

try:
    import blake3 as _b3

    _blake3_mod = _b3
    _BLAKE3_AVAILABLE = True
except ImportError:
    pass

_SMART_HASH_THRESHOLD = 256 * 1024  # 256 KB


def hash_content_py(content: bytes) -> str:
    """Compute content hash (BLAKE3 preferred, SHA-256 fallback)."""
    if _BLAKE3_AVAILABLE:
        result: str = _blake3_mod.blake3(content).hexdigest()
        return result
    return hashlib.sha256(content).hexdigest()


def hash_content_smart_py(content: bytes) -> str:
    """Smart hash with sampling for large files (>256KB)."""
    if len(content) <= _SMART_HASH_THRESHOLD:
        return hash_content_py(content)
    # Sample: first 64KB + middle 64KB + last 64KB + length
    chunk = 64 * 1024
    sample = (
        content[:chunk] + content[len(content) // 2 : len(content) // 2 + chunk] + content[-chunk:]
    )
    if _BLAKE3_AVAILABLE:
        h = _blake3_mod.blake3(sample)
        h.update(len(content).to_bytes(8, "little"))
        smart_result: str = h.hexdigest()
        return smart_result
    h = hashlib.sha256(sample)
    h.update(len(content).to_bytes(8, "little"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# BloomFilter — simple Python set-based implementation
# ---------------------------------------------------------------------------


class BloomFilter:
    """Set-based BloomFilter stand-in (no false positives, uses more memory)."""

    def __init__(self, capacity: int = 10000, fp_rate: float = 0.01) -> None:
        self._capacity = capacity
        self._fp_rate = fp_rate
        self._set: set[str] = set()

    def add(self, key: str) -> None:
        self._set.add(key)

    def add_bulk(self, keys: list[str]) -> None:
        self._set.update(keys)

    def __contains__(self, key: str) -> bool:
        return key in self._set

    @property
    def memory_bytes(self) -> int:
        # Rough estimate: 64 bytes per entry overhead
        return len(self._set) * 64


# ---------------------------------------------------------------------------
# File I/O — pure Python implementations
# ---------------------------------------------------------------------------


def read_file(path: str) -> bytes | None:
    """Read a file from disk, return None if missing or error."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def read_files_bulk(paths: list[str]) -> dict[str, bytes | None]:
    """Read multiple files, returning {path: content_or_None}."""
    result: dict[str, bytes | None] = {}
    for path in paths:
        result[path] = read_file(path)
    return result


# ---------------------------------------------------------------------------
# Grep — pure Python implementations
# ---------------------------------------------------------------------------


def grep_files_mmap(
    pattern: str,
    file_paths: list[str],
    *,
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]]:
    """Grep files using mmap for performance. Returns list of match dicts."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern.encode(), flags)
    except re.error:
        return []

    results: list[dict[str, Any]] = []
    for file_path in file_paths:
        if len(results) >= max_results:
            break
        try:
            with open(file_path, "rb") as f:
                if os.fstat(f.fileno()).st_size == 0:
                    continue
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    for line_num, line in enumerate(iter(mm.readline, b""), 1):
                        if compiled.search(line):
                            results.append(
                                {
                                    "file": file_path,
                                    "line": line_num,
                                    "content": line.decode("utf-8", errors="replace").rstrip("\n"),
                                }
                            )
                            if len(results) >= max_results:
                                break
        except (OSError, ValueError):
            continue
    return results


def grep_bulk(
    pattern: str,
    file_contents: dict[str, bytes],
    *,
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]] | None:
    """Grep pre-loaded file contents. Returns list of match dicts."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern.encode(), flags)
    except re.error:
        return None

    results: list[dict[str, Any]] = []
    for file_path, content in file_contents.items():
        if len(results) >= max_results:
            break
        for line_num, line in enumerate(content.split(b"\n"), 1):
            if compiled.search(line):
                results.append(
                    {
                        "file": file_path,
                        "line": line_num,
                        "content": line.decode("utf-8", errors="replace"),
                    }
                )
                if len(results) >= max_results:
                    break
    return results


# ---------------------------------------------------------------------------
# Glob — pure Python implementation
# ---------------------------------------------------------------------------


def glob_match_bulk(patterns: list[str], paths: list[str]) -> list[str]:
    """Return paths that match any of the glob patterns."""
    import fnmatch

    matched: list[str] = []
    for path in paths:
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
                matched.append(path)
                break
    return matched


# ---------------------------------------------------------------------------
# Prefix / path batch helpers — None sentinels (requires native binary)
# ---------------------------------------------------------------------------

batch_prefix_check = None
any_path_starts_with = None

# ---------------------------------------------------------------------------
# Trigram index — None sentinels (requires native binary)
# ---------------------------------------------------------------------------

build_trigram_index = None
build_trigram_index_from_entries = None
invalidate_trigram_cache = None
trigram_grep = None
trigram_index_stats = None
trigram_search_candidates = None

# ---------------------------------------------------------------------------
# ReBAC fast — None sentinels (requires native binary)
# ---------------------------------------------------------------------------

compute_permission_single = None
compute_permissions_bulk = None
expand_subjects = None
list_objects_for_subject = None

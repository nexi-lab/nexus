"""Human-readable display path utilities for connector-synced files.

Provides filename sanitization, collision resolution, and the
``DisplayPathMixin`` that connectors use to generate human-readable
virtual paths from opaque backend IDs + metadata.

Design decisions (Issue #3256):
    - Sanitization: replace unsafe chars with ``-``, NFC-normalize Unicode,
      cap at 140 chars (rclone/eCryptfs safe limit).
    - Collision resolution: O(n) dict-based grouping, append ``_{hash[:8]}``
      only to duplicates (rclone issue #4412 / google-drive-ocamlfuse #390).
    - DisplayPathMixin: opt-in mixin on CLIConnector, called from both
      CLISyncProvider and SyncPipelineService sync paths.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections import defaultdict
from typing import Any

# ---------------------------------------------------------------------------
# Pre-compiled regex patterns (Issue #3256, Decision 13A)
# ---------------------------------------------------------------------------

# Characters unsafe in filenames across platforms (Windows + POSIX + cloud).
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Collapse runs of dashes/underscores/spaces into a single dash.
_COLLAPSE_SEPS = re.compile(r"[-_\s]+")

# Leading/trailing dots, spaces, and dashes (clean up after collapse).
_LEADING_TRAILING = re.compile(r"^[\s.\-]+|[\s.\-]+$")

# Windows reserved device names (case-insensitive, with or without extension).
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# Maximum safe filename length (eCryptfs / cloud backend limit).
MAX_FILENAME_LEN = 140

# Fallback name when input is empty or entirely unsafe characters.
_FALLBACK_NAME = "_unnamed"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize_filename(name: str, max_len: int = MAX_FILENAME_LEN) -> str:
    """Sanitize a string for use as a filename component.

    Handles:
    - NFC Unicode normalization
    - Unsafe character replacement (``<>:"/\\|?*`` + control chars)
    - Collapse runs of separators into single ``-``
    - Strip leading/trailing dots and spaces
    - Windows reserved name avoidance
    - Length truncation (preserving a hash suffix for uniqueness)

    Args:
        name: Raw string (e.g., email subject, event title).
        max_len: Maximum filename length (default 140).

    Returns:
        Safe, non-empty filename string.
    """
    if not name or not name.strip():
        return _FALLBACK_NAME

    # NFC normalize — canonical composed form (é not e+combining accent).
    result = unicodedata.normalize("NFC", name)

    # Replace unsafe characters with dash.
    result = _UNSAFE_CHARS.sub("-", result)

    # Collapse separators.
    result = _COLLAPSE_SEPS.sub("-", result)

    # Strip leading/trailing junk.
    result = _LEADING_TRAILING.sub("", result)

    # If nothing left after sanitization, use fallback.
    if not result:
        return _FALLBACK_NAME

    # Avoid Windows reserved names.
    stem = result.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        result = f"_{result}"

    # Truncate if too long — keep beginning, append short hash of full name
    # so truncated names from different originals don't collide.
    if len(result) > max_len:
        hash_suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:6]
        result = f"{result[: max_len - 7]}_{hash_suffix}" if max_len >= 8 else hash_suffix[:max_len]

    return result


def resolve_collisions(
    items: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Resolve filename collisions by appending hash suffixes.

    Uses O(n) dict-based grouping. Only items that collide get a suffix;
    unique names stay clean.

    Args:
        items: List of ``(display_path, backend_id)`` tuples.

    Returns:
        List of ``(resolved_path, backend_id)`` tuples with collisions
        disambiguated via ``_{hash[:8]}`` suffix.
    """
    if not items:
        return []

    # Group indices by display_path.
    groups: dict[str, list[int]] = defaultdict(list)
    for i, (path, _bid) in enumerate(items):
        groups[path].append(i)

    result = list(items)
    for path, indices in groups.items():
        if len(indices) <= 1:
            continue
        # Multiple items share the same display path — disambiguate.
        for idx in indices:
            _, bid = items[idx]
            suffix = hashlib.sha256(bid.encode("utf-8")).hexdigest()[:8]
            stem, ext = os.path.splitext(path)
            result[idx] = (f"{stem}_{suffix}{ext}", bid)

    return result


# ---------------------------------------------------------------------------
# DisplayPathMixin
# ---------------------------------------------------------------------------


class DisplayPathMixin:
    """Mixin that generates human-readable VFS paths from backend metadata.

    Connectors override ``display_path()`` to provide content-aware naming.
    The default implementation falls back to ``{item_id}.yaml``.

    Called from both ``CLISyncProvider._parse_list_output()`` and
    ``SyncPipelineService._step1_discover_files()`` to ensure consistent
    naming across both sync paths.
    """

    def display_path(self, item_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Generate a human-readable relative path for a synced item.

        Override in subclasses to provide connector-specific naming.

        Args:
            item_id: Backend-specific item identifier.
            metadata: Item metadata dict (labels, subject, title, dates, etc.).

        Returns:
            Relative path string (e.g., ``INBOX/PRIMARY/2026-03-20_Meeting.yaml``).
        """
        return f"{item_id}.yaml"

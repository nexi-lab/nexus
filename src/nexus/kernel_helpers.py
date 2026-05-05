"""Pre-W3 helpers around ``kernel.metastore_*`` PyO3 methods.

Five proxy methods do more than 1:1 forward to the kernel:

    * ``set_file_metadata(p, k, v)`` — JSON-encode non-string ``value``
    * ``get_searchable_text_bulk(paths)`` — drop ``None`` entries from bulk fetch
    * ``list(prefix, recursive)`` / ``list_iter(...)`` — recursive=False post-filter
    * ``list_paginated(...)`` — wrap raw dict in ``PaginatedResult`` dataclass

Extracting them as free functions lets call sites migrate off the
``RustMetastoreProxy`` class one helper at a time as W1/W2 dismantle
the proxy. Each helper takes the kernel as the first argument and is
otherwise byte-for-byte identical to the proxy method it replaces.

After W3 deletes ``RustMetastoreProxy``, these helpers are the only
non-trivial layer between Python callers and ``kernel.metastore_*``.
The trivial 1:1 forwards (``get`` / ``put`` / ``delete`` / ``exists`` /
``rename_path`` / ``put_if_version`` / ``is_implicit_directory`` /
``get_file_metadata*``) need no helpers; callers go direct.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata

logger = logging.getLogger(__name__)


def _is_direct_child(path: str, prefix: str) -> bool:
    """Return True when ``path`` is an immediate child of ``prefix``.

    Strip the prefix, then drop any entry whose remainder still contains
    a ``/`` (i.e. sits in a deeper subdirectory). Used to honour
    ``recursive=False`` on the listing helpers.
    """
    rel = path[len(prefix) :].lstrip("/") if path.startswith(prefix) else path
    return "/" not in rel


def metastore_set_file_metadata(kernel: Any, path: str, key: str, value: Any) -> None:
    """JSON-encode non-string ``value`` before persisting.

    The kernel boundary stores strings; non-string callers (parser hooks
    storing structured tag blobs) would otherwise get a PyErr from the
    PyO3 binding. ``None`` is the parser-hook sentinel for "clear this
    field" — the kernel treats absent and ``None`` identically, so we
    do nothing.
    """
    if value is None:
        return
    if not isinstance(value, str):
        import json

        value = json.dumps(value)
    kernel.set_xattr(path, key, value)


def metastore_get_searchable_text_bulk(
    kernel: Any,
    paths: Sequence[str],
) -> dict[str, str]:
    """Return cached ``parsed_text`` for the given paths, dropping Nones.

    F3 C2 wired ``parsed_text`` storage into the kernel's file_metadata
    side-car; this call fans out to
    ``kernel.metastore_get_file_metadata_bulk`` and drops paths with no
    cached text so search_service grep / pipeline_indexer fall through
    to the raw-content path for un-parsed files.
    """
    bulk = kernel.get_xattr_bulk(list(paths), "parsed_text")
    return {p: v for p, v in bulk.items() if v is not None}


def metastore_list(
    kernel: Any,
    prefix: str = "",
    recursive: bool = True,
) -> list[FileMetadata]:
    """List with Python-side ``recursive=False`` post-filter.

    The Rust ``metastore_list`` is prefix-only — it returns every entry
    whose path starts with ``prefix``. When the caller asks for
    ``recursive=False`` we drop entries that sit deeper than one
    separator below the prefix.
    """
    result: list[FileMetadata] = kernel.metastore_list(prefix)
    if recursive:
        return result
    return [e for e in result if _is_direct_child(e.path, prefix)]


def metastore_list_iter(
    kernel: Any,
    prefix: str = "",
    recursive: bool = True,
) -> Iterator[FileMetadata]:
    """Streaming variant of :func:`metastore_list`.

    Yields entries one at a time, honouring the same ``recursive=False``
    post-filter. The Rust call still materialises the full prefix list
    today; the helper exists so future kernel-side streaming is a
    drop-in here without rewiring call sites.
    """
    for e in kernel.metastore_list(prefix):
        if recursive or _is_direct_child(e.path, prefix):
            yield e


def metastore_list_paginated(
    kernel: Any,
    prefix: str = "",
    recursive: bool = True,
    limit: int = 1000,
    cursor: str | None = None,
) -> Any:
    """Wrap ``kernel.metastore_list_paginated`` dict in ``PaginatedResult``.

    The kernel returns ``{items, next_cursor, has_more, total_count}``
    as a plain dict; callers want attribute access (``.items`` /
    ``.next_cursor``) for mypy + IDE completion, so we wrap it in the
    dataclass.
    """
    from nexus.core.pagination import PaginatedResult

    page = kernel.metastore_list_paginated(prefix, recursive, limit, cursor)
    return PaginatedResult(
        items=page["items"],
        next_cursor=page["next_cursor"],
        has_more=page["has_more"],
        total_count=page["total_count"],
    )


# ── parsed_text-unavailable warning shim ─────────────────────────────────
#
# RustMetastoreProxy never had an xattr surface, so ``parsed_text`` (set
# by the auto_parse hook on other metastores for binary docs like
# ``.pdf`` / ``.docx``) is not available here. ``grep`` then falls back
# to raw-byte reads, which skip non-UTF8 content. The shim below emits
# a single warning so an operator looking into a "why didn't grep find
# anything in this PDF" incident has something to grep for, then
# returns ``None`` so callers' ``hasattr``/``is None`` checks pass
# through cleanly.
_PARSED_TEXT_WARNING_EMITTED = False


def _warn_parsed_text_unavailable_once() -> None:
    global _PARSED_TEXT_WARNING_EMITTED
    if not _PARSED_TEXT_WARNING_EMITTED:
        _PARSED_TEXT_WARNING_EMITTED = True
        logger.warning(
            "[kernel_helpers] parsed_text xattr cache is not available on "
            "the Rust metastore — grep/index calls for parseable binaries "
            "(.pdf/.docx/.xlsx) will fall back to raw bytes and skip non-"
            "UTF8 payloads. Tracked as a SANDBOX follow-up."
        )


def metastore_get_searchable_text(kernel: Any, path: str) -> str | None:
    """Return cached ``parsed_text`` for *path*, or ``None`` if absent.

    The kernel side-car stores ``parsed_text`` via
    ``metastore_set_file_metadata(path, "parsed_text", text)``; this
    helper is a thin lookup over ``metastore_get_file_metadata`` so
    indexer fast-path callers don't have to know the key.

    Returns ``None`` for paths with no cached text (callers fall
    through to raw ``sys_read``). When the kernel side-car itself is
    unavailable (legacy boots that haven't wired the file_metadata
    table yet), emits a one-time warning so an operator looking into
    a "why is grep slow" incident has something to grep.
    """
    try:
        result = kernel.get_xattr(path, "parsed_text")
    except AttributeError:
        _warn_parsed_text_unavailable_once()
        return None
    if result is None:
        return None
    return result if isinstance(result, str) else str(result)

"""Write-to-searchable helpers (#4736).

Shared by ``POST /files/write`` and ``POST /files/batch/write`` (the
optional ``index`` option).
These helpers pass text the server already holds to ``IndexDocuments``
and translate the plugin's
aggregate answer back into a per-path verdict the caller can act on.

Underscore-prefixed sibling of the router modules (same convention as
``_search_deps``); import from the routers only.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    AccessDeniedError,
    NexusFileNotFoundError,
    NexusPermissionError,
)

logger = logging.getLogger(__name__)

STATUS_INDEXED: Final = "indexed"
STATUS_SKIPPED: Final = "skipped"
STATUS_ERROR: Final = "error"

REASON_NON_TEXT = "content is not valid UTF-8 text; pass index.text with extracted text to index it"
REASON_EMPTY = "empty or whitespace-only content; nothing to index"
REASON_OVERSIZE = (
    "content exceeds the inline indexing cap; index it with `nexus search index <dir>` "
    "(plugin-side walk)"
)
REASON_NOT_A_PATH = "not a VFS path (entity URN or relative name); nothing to index"

_SKIP_KIND: Final[dict[str, str]] = {
    REASON_EMPTY: "empty",
    REASON_NON_TEXT: "non_text",
    REASON_OVERSIZE: "oversize",
    REASON_NOT_A_PATH: "not_a_path",
}

_PERMISSION_ERRORS = (NexusPermissionError, AccessDeniedError, PermissionError)


class IndexOnWrite(BaseModel):
    """Object form of the ``index`` write option."""

    text: str | None = Field(
        None,
        description=(
            "Text to index instead of the written bytes (e.g. extracted text for a "
            "PDF or image). Omit to index the written content decoded as UTF-8."
        ),
    )


IndexOption = bool | IndexOnWrite | None


class WriteIndexResult(BaseModel):
    """Per-path outcome of index-on-write.

    ``index_seq`` is the plugin sequence stamped after the commit that made
    the document searchable — ``/v2/documents/stats`` ``last_index_seq >=
    index_seq`` means it is served.  ``reason`` explains a ``skipped``
    verdict; ``error`` carries the plugin failure text behind ``error``.
    """

    status: Literal["indexed", "skipped", "error"]
    index_seq: int | None = None
    reason: str | None = None
    error: str | None = None


def index_zone_for(auth_result: dict[str, Any] | None, *, override: str | None = None) -> str:
    """The zone selected by the authenticated Python write/reindex request.

    ``override`` is the files routes' ``?zone=`` parameter, already gated
    by ``_apply_zone_override``. The host enforces management authority.
    """
    if override:
        return override
    return (auth_result or {}).get("zone_id") or ROOT_ZONE_ID


def index_requested(option: IndexOption) -> bool:
    return option is True or isinstance(option, IndexOnWrite)


def effective_index_option(item_option: IndexOption, request_default: bool | None) -> IndexOption:
    """Per-item ``index`` wins; otherwise the request-level default."""
    return item_option if item_option is not None else request_default


def require_search_daemon(app_state: Any) -> Any:
    """The search daemon, or 503.

    Checked BEFORE the write so a request that asked for indexing never
    half-succeeds silently: either both the write and the index happen,
    or nothing is written and the caller sees the 503.
    """
    daemon = getattr(app_state, "search_daemon", None)
    if daemon is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "index requested but the search daemon is unavailable; nothing was written. "
                "Retry without `index`, or load nexus-search-plugin "
                "(docs/deployment/search-plugin.md)."
            ),
        )
    return daemon


def decode_index_text(content: bytes) -> str | None:
    """Strict UTF-8 decode; ``None`` means a binary payload the plugin cannot chunk."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def index_text_for(option: IndexOption, content: bytes) -> str | None:
    """Text to index for a written file: the explicit override or the decoded bytes."""
    if isinstance(option, IndexOnWrite) and option.text is not None:
        return option.text
    return decode_index_text(content)


def to_epoch_ms(value: Any) -> int | None:
    """``modified_at`` from a VFS write / stat result as ms since epoch.

    Recorded on the plugin side so a later ``Refresh`` walk verdicts the
    file Unchanged instead of re-embedding it.  Unknown shapes yield
    ``None`` (the plugin then always re-verdicts Changed), never an error.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(aware.timestamp() * 1000)
    if isinstance(value, int | float):
        # Epoch seconds sit near 1.7e9; epoch milliseconds near 1.7e12.
        return int(value * 1000) if value < 1e12 else int(value)
    if isinstance(value, str):
        try:
            return to_epoch_ms(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def build_document(path: str, text: str, *, mtime_ms: int | None) -> dict[str, Any]:
    doc: dict[str, Any] = {"path": path, "text": text}
    if mtime_ms is not None:
        doc["mtime_ms"] = mtime_ms
    return doc


def verdict_for_path(result: Any, path: str) -> WriteIndexResult:
    """Per-path verdict from a ``SearchDaemon.index_documents`` result.

    The plugin reports content skips (empty / whitespace-only / chunkless)
    as ``skipped_paths``; everything else in the batch was committed and
    carries the batch's ``index_seq``.
    """
    if isinstance(result, dict):
        skipped_paths = result.get("skipped_paths") or []
        index_seq = result.get("index_seq")
    else:
        skipped_paths = getattr(result, "skipped_paths", None) or []
        index_seq = getattr(result, "index_seq", None)
    if path in skipped_paths:
        return WriteIndexResult(status=STATUS_SKIPPED, reason=REASON_EMPTY)
    return WriteIndexResult(
        status=STATUS_INDEXED,
        index_seq=int(index_seq) if index_seq is not None else None,
    )


async def index_documents_for_paths(
    search_daemon: Any,
    docs: Sequence[dict[str, Any]],
    *,
    zone_id: str | None,
) -> dict[str, WriteIndexResult]:
    """One ``IndexDocuments`` round-trip for ``docs``; verdict per path.

    A plugin failure yields ``status: "error"`` for every path rather
    than raising: the writes already landed, so the caller re-drives
    indexing via ``/v2/documents/batch`` instead of retrying the write.
    """
    if not docs:
        return {}
    try:
        result = await search_daemon.index_documents(list(docs), zone_id=zone_id)
    except Exception as exc:
        logger.error(
            "index-on-write: index_documents failed for %d document(s): %s",
            len(docs),
            exc,
            exc_info=True,
        )
        error = f"{type(exc).__name__}: {exc}"
        return {doc["path"]: WriteIndexResult(status=STATUS_ERROR, error=error) for doc in docs}
    return {doc["path"]: verdict_for_path(result, doc["path"]) for doc in docs}


async def index_after_write(
    search_daemon: Any,
    *,
    path: str,
    content: bytes,
    option: IndexOption,
    modified_at: Any,
    zone_id: str | None,
) -> WriteIndexResult:
    """Index one just-written file; called only when ``index_requested(option)``."""
    text = index_text_for(option, content)
    if text is None:
        return WriteIndexResult(status=STATUS_SKIPPED, reason=REASON_NON_TEXT)
    verdicts = await index_documents_for_paths(
        search_daemon,
        [build_document(path, text, mtime_ms=to_epoch_ms(modified_at))],
        zone_id=zone_id,
    )
    return verdicts[path]


async def index_after_batch_write(
    search_daemon: Any,
    entries: Sequence[tuple[str, bytes, IndexOption, Any]],
    *,
    zone_id: str | None,
) -> dict[str, WriteIndexResult]:
    """Index the requested subset of a batch in ONE plugin round-trip.

    ``entries`` are ``(path, written bytes, effective index option,
    modified_at)``; only entries whose option requests indexing get a
    verdict.  Non-text payloads are skipped locally without reaching
    the plugin.
    """
    verdicts: dict[str, WriteIndexResult] = {}
    docs: list[dict[str, Any]] = []
    for path, content, option, modified_at in entries:
        if not index_requested(option):
            continue
        text = index_text_for(option, content)
        if text is None:
            verdicts[path] = WriteIndexResult(status=STATUS_SKIPPED, reason=REASON_NON_TEXT)
            continue
        docs.append(build_document(path, text, mtime_ms=to_epoch_ms(modified_at)))
    verdicts.update(await index_documents_for_paths(search_daemon, docs, zone_id=zone_id))
    return verdicts


async def read_written_bytes(fs: Any, path: str, context: Any) -> tuple[bytes, int | None]:
    """Read ``path`` through the VFS with the caller's context.

    Returns the raw bytes plus the stat ``modified_at_ms`` (``None`` when
    unavailable).  Maps VFS errors onto the HTTP codes the files routes
    use: 404 missing, 403 denied, 400 for a directory.
    """
    try:
        # Blocking kernel round-trip — off the event loop (#4777).
        raw = await asyncio.to_thread(fs.read, path, context=context)
        if inspect.isawaitable(raw):
            raw = await raw
    except NexusFileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"path not found: {path}") from exc
    except _PERMISSION_ERRORS as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except IsADirectoryError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{path} is a directory; refresh indexes one regular file",
        ) from exc
    if isinstance(raw, dict):
        # ``return_metadata`` / DT_STREAM shapes wrap the bytes.
        raw = raw.get("content", raw.get("data", b""))
    content = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")

    mtime_ms: int | None = None
    try:
        meta = await asyncio.to_thread(fs.sys_stat, path, context=context)
        if inspect.isawaitable(meta):
            meta = await meta
        if isinstance(meta, dict):
            mtime_ms = to_epoch_ms(meta.get("modified_at_ms"))
    except Exception:  # mtime only speeds up a later Refresh; never a reason to fail
        mtime_ms = None
    return content, mtime_ms

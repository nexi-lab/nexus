"""Write-to-searchable helpers (#4736).

Shared by ``POST /files/write`` and ``POST /files/batch/write`` (the
optional ``index`` option) and by ``POST /search/refresh``.  Post-P12 the
ONLY path that puts text into the Rust search plugin is ``IndexDocuments``
with the text inline: a VFS write does not index, and ``NotifyFileChange``
for ``create`` / ``update`` is a no-op ack.  These helpers turn "bytes the
server already holds" into that one call and translate the plugin's
aggregate answer back into a per-path verdict the caller can act on.

Underscore-prefixed sibling of the router modules (same convention as
``_search_deps``); import from the routers only.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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

# Bulk reindex (admin /reindex) batching.  IndexDocuments carries the text
# inline and the plugin's tonic server decodes at most 4 MiB per message, so
# a batch stays well under that; one oversize file is skipped with a reason
# instead of failing the whole pass.
REINDEX_MAX_DOC_BYTES = 2 * 1024 * 1024
REINDEX_BATCH_DOCS = 64
REINDEX_BATCH_BYTES = 2 * 1024 * 1024
# Response-body cap for per-path lists (matches the pre-existing 25).
REINDEX_PATH_LIST_CAP = 25

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
    """Per-path outcome of index-on-write / refresh.

    ``index_seq`` is the plugin sequence stamped after the commit that made
    the document searchable — ``/search/stats`` ``last_index_seq >=
    index_seq`` means it is served.  ``reason`` explains a ``skipped``
    verdict; ``error`` carries the plugin failure text behind ``error``.
    """

    status: Literal["indexed", "skipped", "error"]
    index_seq: int | None = None
    reason: str | None = None
    error: str | None = None


def index_zone_for(auth_result: dict[str, Any] | None, *, override: str | None = None) -> str:
    """The plugin zone every indexing call writes — and ``/search/query`` reads.

    ``/search/query`` scopes to the token's ``zone_id`` (ROOT when absent),
    so indexing anywhere else would leave a document invisible to the
    caller's own queries.  ``/search/index``, ``/search/refresh``,
    ``files/write`` + ``files/batch/write`` (``index: true``) and
    ``/search/stats`` all derive their zone here so the four surfaces
    cannot drift.  ``override`` is the files routes' ``?zone=`` parameter,
    already gated by ``_apply_zone_override``.
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
    indexing via ``/search/index`` instead of retrying the write.
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
        raw = fs.read(path, context=context)
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
        meta = fs.sys_stat(path, context=context)
        if inspect.isawaitable(meta):
            meta = await meta
        if isinstance(meta, dict):
            mtime_ms = to_epoch_ms(meta.get("modified_at_ms"))
    except Exception:  # mtime only speeds up a later Refresh; never a reason to fail
        mtime_ms = None
    return content, mtime_ms


# ── Bulk reindex (POST /api/v2/admin/reindex, #4241 / #4736) ─────────


@dataclass
class ReindexSearchOutcome:
    """Per-path accounting for a bulk search reindex.

    Post-P12 the plugin indexes SYNCHRONOUSLY and ``NotifyFileChange`` for
    create / update is a no-op ack, so there is no queue to report.  Every
    replayed path lands in exactly one bucket: ``indexed``, ``deleted``
    (evicted), ``skipped`` (with a reason histogram) or ``errors``.
    """

    indexed: int = 0
    deleted: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    skipped_paths: list[str] = field(default_factory=list)
    errors: int = 0
    failed_paths: list[str] = field(default_factory=list)
    first_error: str | None = None
    #: Highest plugin ``index_seq`` seen — ``/search/stats`` ``last_index_seq
    #: >= index_seq`` means everything this pass indexed is served.
    index_seq: int | None = None
    #: Epoch seconds when the last plugin call returned (``None`` if none ran).
    completed_at: float | None = None

    def skip(self, path: str, reason: str) -> None:
        self.skipped += 1
        kind = _SKIP_KIND.get(reason, "other")
        self.skip_reasons[kind] = self.skip_reasons.get(kind, 0) + 1
        if len(self.skipped_paths) < REINDEX_PATH_LIST_CAP:
            self.skipped_paths.append(path)

    def fail(self, path: str, error: str) -> None:
        self.errors += 1
        if len(self.failed_paths) < REINDEX_PATH_LIST_CAP:
            self.failed_paths.append(path)
        if self.first_error is None:
            self.first_error = error

    def note_seq(self, seq: int | None) -> None:
        if seq:
            self.index_seq = max(self.index_seq or 0, int(seq))
        self.completed_at = time.time()

    @classmethod
    def unavailable(cls, paths: Mapping[str, str], reason: str) -> ReindexSearchOutcome:
        """Every path failed because the plugin / VFS could not be reached."""
        out = cls()
        for path in paths:
            out.fail(path, reason)
        return out


async def reindex_paths(
    search_daemon: Any,
    fs: Any,
    context: Any,
    changes: Mapping[str, str],
    *,
    zone_id: str,
) -> ReindexSearchOutcome:
    """Drive the search plugin for every path a reindex replay touched.

    ``changes`` maps path → ``"delete"`` | ``"update"``.  Deletes evict via
    ``NotifyFileChange``; updates are read through the VFS with ``context``
    (the admin caller) and indexed in bounded ``IndexDocuments`` batches
    (``REINDEX_BATCH_DOCS`` docs / ``REINDEX_BATCH_BYTES`` bytes).  A
    single unreadable, oversize or non-text file never aborts the pass — it
    is counted and named in the outcome.
    """
    out = ReindexSearchOutcome()
    batch: list[dict[str, Any]] = []
    batch_bytes = 0

    async def _flush() -> None:
        nonlocal batch, batch_bytes
        if not batch:
            return
        verdicts = await index_documents_for_paths(search_daemon, batch, zone_id=zone_id)
        for doc in batch:
            verdict = verdicts[doc["path"]]
            if verdict.status == STATUS_INDEXED:
                out.indexed += 1
                out.note_seq(verdict.index_seq)
            elif verdict.status == STATUS_SKIPPED:
                out.skip(doc["path"], verdict.reason or REASON_EMPTY)
            else:
                out.fail(doc["path"], verdict.error or "index failed")
        out.completed_at = time.time()
        batch = []
        batch_bytes = 0

    for path, change in changes.items():
        if not path.startswith("/"):
            # Defensive twin of the route-side filter: an entity URN or
            # relative name is not a document, so it is neither read nor
            # evicted — and never a failure.
            out.skip(path, REASON_NOT_A_PATH)
            continue
        if change == "delete":
            try:
                result = await search_daemon.notify_file_change(path, "delete", zone_id=zone_id)
            except Exception as exc:
                logger.warning("reindex: evict failed for %s: %s", path, exc)
                out.fail(path, f"{type(exc).__name__}: {exc}")
                continue
            out.deleted += 1
            out.note_seq(result.get("index_seq") if isinstance(result, dict) else None)
            continue

        try:
            content, mtime_ms = await read_written_bytes(fs, path, context)
        except HTTPException as exc:
            out.fail(path, f"read failed ({exc.status_code}): {exc.detail}")
            continue
        except Exception as exc:
            logger.warning("reindex: read failed for %s: %s", path, exc)
            out.fail(path, f"read failed: {type(exc).__name__}: {exc}")
            continue
        if len(content) > REINDEX_MAX_DOC_BYTES:
            out.skip(path, REASON_OVERSIZE)
            continue
        text = decode_index_text(content)
        if text is None:
            out.skip(path, REASON_NON_TEXT)
            continue
        if batch and (
            len(batch) >= REINDEX_BATCH_DOCS or batch_bytes + len(content) > REINDEX_BATCH_BYTES
        ):
            await _flush()
        batch.append(build_document(path, text, mtime_ms=mtime_ms))
        batch_bytes += len(content)

    await _flush()
    return out

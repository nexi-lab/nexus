"""Factory adapters — NexusFSFileReader, DaemonSkeletonBM25, WorkflowLifecycleAdapter."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from nexus.lib.virtual_views import is_parseable_path

logger = logging.getLogger(__name__)

# =========================================================================
# Issue #1520: NexusFS → FileReaderProtocol adapter
# =========================================================================


def _sanitize_for_index(text: str) -> str:
    """Strip NUL bytes so Postgres text columns accept the string.

    PDF parsers can emit embedded NUL (0x00) from stream artifacts.  Postgres
    rejects those in ``TEXT``/``VARCHAR`` values (SQLSTATE 22021), and search
    indexing writes through SQLAlchemy text columns, so a single NUL anywhere
    in a parsed document would roll back the whole write transaction.
    """
    return text.replace("\x00", "") if "\x00" in text else text


def _apply_parse_transform(
    nx: Any,
    path: str,
    raw: Any,
    *,
    parse_fn: Callable[[bytes, str], bytes | None] | None,
) -> str:
    """Pure sync transform: raw bytes → parsed-and-sanitized searchable text.

    Shared by ``_NexusFSFileReader.read_text`` (daemon refresh path) and
    ``handle_semantic_search_index`` (RPC path).  The caller owns the read
    — including the ``OperationContext`` / ReBAC scope — so this function
    never touches the filesystem itself.

    Fail-closed: for a parseable binary with no working parser or a parse
    error, returns ``""`` so the indexer skips the file instead of
    embedding utf-8 garbage.
    """
    text, _status = _apply_parse_transform_with_status(nx, path, raw, parse_fn=parse_fn)
    return text


def _apply_parse_transform_with_status(
    nx: Any,
    path: str,
    raw: Any,
    *,
    parse_fn: Callable[[bytes, str], bytes | None] | None,
) -> tuple[str, str]:
    """Variant of :func:`_apply_parse_transform` that also reports status.

    Returns ``(text, status)`` where ``status`` is one of:
      * ``"ok"`` — non-empty text produced (either from a parse run or a
        direct utf-8 decode for non-parseable paths).
      * ``"empty"`` — parseable file that parsed successfully but yielded
        zero extractable text (image-only PDF, blank .docx, …).  Also
        returned for non-parseable files whose decoded content happens to
        be empty.
      * ``"error"`` — parseable file where parsing failed (parser missing,
        raised, or returned ``None``).  The RPC stale-doc purge path uses
        this to avoid deleting healthy docs during transient parser
        outages — only ``"empty"`` is a reliable stale signal.
    """
    if is_parseable_path(path):
        raw_bytes = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", errors="ignore")
        parsed = _get_parsed_text_sync(nx, path, raw_bytes, parse_fn=parse_fn)
        if parsed is None:
            return "", "error"
        text = _sanitize_for_index(parsed)
        return text, ("empty" if not text else "ok")

    if isinstance(raw, bytes):
        decoded = _sanitize_for_index(raw.decode("utf-8", errors="ignore"))
    else:
        decoded = _sanitize_for_index(str(raw))
    return decoded, ("empty" if not decoded else "ok")


def _compute_content_hash(raw: bytes) -> str:
    """Hash raw bytes the same way the kernel hashes ``file_paths.content_hash``.

    Using the kernel's BLAKE3 helper (with the same fallback chain) means
    the adapter's ``parsed_text_hash`` key aligns with
    ``file_paths.content_hash``, so downstream consumers (e.g.
    ``IndexingService``) can cross-reference the two without running a
    second hash pass on the raw bytes.
    """
    from nexus.core.hash_fast import hash_content

    return hash_content(raw)


def _get_parsed_text_sync(
    nx: Any,
    path: str,
    raw: bytes,
    *,
    parse_fn: Callable[[bytes, str], bytes | None] | None,
) -> str | None:
    """Sync flavor of ``_NexusFSFileReader._get_parsed_text`` used by RPC path.

    ``parse_fn`` already runs in a worker thread under the ParsersBrick
    wrapper when an event loop is active, so blocking inside the RPC
    handler for one document at a time is acceptable; we don't need a
    second ``asyncio.to_thread`` hop here.

    Return value:
      * ``None`` — parse error (parser missing, raised, or returned None).
        Caller should fail-closed.
      * ``""`` — parse succeeded but yielded no extractable text (image-only
        PDF, scanned doc, blank page).  A cache entry IS written so the
        indexer can distinguish this case from a parse error via the
        presence of a matching ``parsed_text_hash``.
      * non-empty string — successfully parsed markdown.
    """
    # Content-hash the raw bytes so the cache entry is zone-aware and
    # revision-aware.  File metadata is keyed by path alone in the default
    # metastore; two zones with different ``/report.pdf`` files, or the same
    # file rewritten with fresh bytes, would otherwise collide on the path
    # key and one would silently serve the other's parsed text.
    try:
        raw_hash = _compute_content_hash(raw)
    except Exception:
        # Hashing itself never fails in normal operation; but if BLAKE3 is
        # broken, indexing the file without cache safety is worse than
        # skipping — fail closed.
        logger.warning(
            "parse transform: content-hash computation failed for %s", path, exc_info=True
        )
        return None

    try:
        cached = nx.metadata.get_file_metadata(path, "parsed_text")
        cached_hash = nx.metadata.get_file_metadata(path, "parsed_text_hash")
        if cached_hash == raw_hash and cached is not None:
            cached_str = (
                cached if isinstance(cached, str) else cached.decode("utf-8", errors="ignore")
            )
            return _sanitize_for_index(cached_str)
    except Exception:
        pass

    if parse_fn is None:
        logger.warning(
            "parse transform: no parse_fn wired for parseable binary %s — "
            "skipping instead of indexing raw bytes",
            path,
        )
        return None
    try:
        parsed_bytes = parse_fn(raw, path)
    except Exception:
        logger.warning("parse transform: parse_fn raised for %s", path, exc_info=True)
        return None
    if parsed_bytes is None:
        logger.warning("parse transform: parse_fn returned None for %s", path)
        return None
    # ``parsed_bytes == b""`` is a SUCCESSFUL parse that yielded zero
    # extractable text (image-only/blank PDFs, scanned docs).  Cache the
    # empty string with the hash companion so the indexer recognizes this
    # as a valid empty parse instead of retrying forever.
    text = _sanitize_for_index(parsed_bytes.decode("utf-8", errors="ignore"))
    logger.info("parse transform: parsed %s → %d chars markdown", path, len(text))

    try:
        from datetime import UTC, datetime

        nx.metadata.set_file_metadata(path, "parsed_text", text)
        nx.metadata.set_file_metadata(path, "parsed_text_hash", raw_hash)
        nx.metadata.set_file_metadata(path, "parsed_at", datetime.now(UTC).isoformat())
    except Exception:
        pass

    return text


class _NexusFSFileReader:
    """Adapts a NexusFS instance to the FileReaderProtocol interface.

    This adapter is the sole coupling point between the kernel (NexusFS)
    and the search brick. Search modules never import NexusFS directly;
    they receive a FileReaderProtocol at composition time.

    Usage::

        from nexus.factory.adapters import _NexusFSFileReader

        reader = _NexusFSFileReader(nexus_fs_instance)
        content = reader.read_text("/path/to/file.py")
    """

    def __init__(
        self,
        nx: Any,
        parse_fn: Callable[[bytes, str], bytes | None] | None = None,
    ) -> None:
        self._nx = nx
        self._parse_fn = parse_fn

    async def read_text(self, path: str) -> str:
        # Read with admin context so the search daemon can index all files
        # regardless of per-user ReBAC permissions.
        from nexus.contracts.types import OperationContext

        admin_ctx = OperationContext(
            user_id="system",
            groups=[],
            is_admin=True,
            is_system=True,
        )
        content_raw = self._nx.sys_read(path, context=admin_ctx)

        # Offload the parse transform to a worker thread — parse_fn is sync
        # and can block the event loop on large PDFs, pinning unrelated
        # search/RPC traffic while one document parses.
        return await asyncio.to_thread(
            _apply_parse_transform,
            self._nx,
            path,
            content_raw,
            parse_fn=self._parse_fn,
        )

    def has_successful_parse(self, path: str, content_hash: str) -> bool:
        """Return True if the metastore records a successful parse at ``content_hash``.

        Used by the indexer to distinguish ``read_text`` returning ``""``
        for a parseable file because:
          * the parser was broken / file unsupported — no matching cache
            pair; caller should retry on the next tick, and
          * the parse ran successfully but the file legitimately yielded
            zero text (image-only PDF, blank page, scanned doc awaiting
            OCR) — the adapter cached the empty parse; caller should
            advance ``indexed_content_hash`` and skip retrying.

        Proof requires BOTH a matching ``parsed_text_hash`` AND a non-None
        ``parsed_text`` value.  The ``parsed_text`` presence check guards
        against stale-hash latching: ``AutoParseWriteHook.on_post_write``
        invalidates the cached text on every write, and on a revert-to-
        previous-revision (same bytes → same hash) a lingering hash
        without its text companion would otherwise convince us an empty
        read was a successful empty parse.

        ``content_hash`` is expected to be the kernel-computed hash from
        ``file_paths.content_hash`` (BLAKE3).  The adapter writes the same
        hash under ``parsed_text_hash``, so matching values prove the parse
        ran against the current bytes.
        """
        try:
            cached_hash = self._nx.metadata.get_file_metadata(path, "parsed_text_hash")
            if not cached_hash or cached_hash != content_hash:
                return False
            cached_text = self._nx.metadata.get_file_metadata(path, "parsed_text")
        except Exception:
            return False
        return cached_text is not None

    def get_searchable_text(self, path: str) -> str | None:
        # Sanitize on read — older cache entries (written before the adapter's
        # write-path sanitizer existed, or by ContentParserEngine which still
        # stores ``result.text`` verbatim) can carry embedded NULs.  The
        # indexing pipeline reads this fast path before falling back to
        # ``read_text``, so without scrubbing here a poisoned cache entry
        # rolls back the Postgres write transaction.
        #
        # Parseable binaries (.pdf/.docx/.xlsx/…) MUST NOT take the fast path:
        # the metastore entry is keyed by path alone, so without a hash
        # companion check we can't prove the cached markdown matches the
        # current bytes.  Cross-zone metadata collisions and rewrite-before-
        # reindex races could otherwise re-index stale text and then latch
        # it as "current" when ``IndexingService`` advances
        # ``indexed_content_hash``.  Fall through to ``read_text`` which
        # validates the cache against a sha256 of the raw bytes.
        if is_parseable_path(path):
            return None
        result: str | None = self._nx.metadata.get_searchable_text(path)
        if result is None:
            return None
        return _sanitize_for_index(result if isinstance(result, str) else str(result))

    async def list_files(self, path: str, recursive: bool = True) -> list[Any]:
        # Read with admin context so the search daemon can index all files
        # regardless of per-user ReBAC permissions (same as read_text).
        from nexus.contracts.types import OperationContext

        admin_ctx = OperationContext(
            user_id="system",
            groups=[],
            is_admin=True,
            is_system=True,
        )
        result = self._nx.sys_readdir(path, recursive=recursive, context=admin_ctx)
        items: list[Any] = result.items if hasattr(result, "items") else result
        return items

    def get_session(self) -> Any:
        return self._nx.SessionLocal()

    def get_path_id(self, path: str, session: Any = None) -> str | None:
        """Look up path_id, optionally reusing an existing session."""
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        stmt = select(FilePathModel.path_id).where(
            FilePathModel.virtual_path == path,
            FilePathModel.deleted_at.is_(None),
        )
        if session is not None:
            path_id: str | None = session.execute(stmt).scalar_one_or_none()
            return path_id
        with self._nx.SessionLocal() as s:
            path_id = s.execute(stmt).scalar_one_or_none()
            return path_id

    def get_content_hash(self, path: str, session: Any = None) -> str | None:
        """Look up content_hash, optionally reusing an existing session."""
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        stmt = select(FilePathModel.content_hash).where(
            FilePathModel.virtual_path == path,
            FilePathModel.deleted_at.is_(None),
        )
        if session is not None:
            content_hash: str | None = session.execute(stmt).scalar_one_or_none()
            return content_hash
        with self._nx.SessionLocal() as s:
            content_hash = s.execute(stmt).scalar_one_or_none()
            return content_hash

    async def read_head(self, path: str, max_bytes: int) -> bytes:
        """Read first max_bytes of a file for skeleton title extraction (Issue #3725).

        Uses admin context so the skeleton indexer can read all files regardless
        of per-user ReBAC permissions (same policy as read_text).
        Returns b'' on any error (file missing, permissions, etc.).
        """
        from nexus.contracts.types import OperationContext

        admin_ctx = OperationContext(
            user_id="system",
            groups=[],
            is_admin=True,
            is_system=True,
        )
        try:
            content = await self._nx.sys_read(path, count=max_bytes, context=admin_ctx)
            if isinstance(content, bytes):
                return content
            return str(content).encode("utf-8", errors="ignore")
        except Exception:
            return b""


# =========================================================================
# Issue #3725: SearchDaemon → SkeletonBM25Protocol adapter
# =========================================================================


class _DaemonSkeletonBM25:
    """Adapts SearchDaemon.upsert_skeleton_doc / delete_skeleton_doc to the
    SkeletonBM25Protocol expected by SkeletonIndexer.

    This is the sole coupling point between the factory layer and the
    SearchDaemon's in-memory skeleton index.  Search modules never import
    SearchDaemon directly; they receive a SkeletonBM25Protocol at
    composition time.
    """

    def __init__(self, daemon: Any) -> None:
        self._daemon = daemon

    async def upsert_skeleton(
        self,
        doc_id: str,  # noqa: ARG002 — Protocol positional arg; daemon uses virtual_path as key
        virtual_path: str,
        title: "str | None",
        zone_id: str,
        *,
        path_id: "str | None" = None,
    ) -> None:
        """Forward upsert to daemon's in-memory index (sync under the hood)."""
        self._daemon.upsert_skeleton_doc(
            # Fall back to virtual_path when path_id UUID is unavailable so the
            # dict entry is still created and locate() works.  Bootstrap from DB
            # restores real UUIDs on restart.
            path_id=path_id or virtual_path,
            virtual_path=virtual_path,
            title=title,
            zone_id=zone_id,
        )

    async def delete_skeleton(self, doc_id: str, zone_id: str) -> None:
        """Forward deletion to daemon's in-memory index."""
        self._daemon.delete_skeleton_doc(virtual_path=doc_id, zone_id=zone_id)

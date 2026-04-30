"""Unified indexing service consolidating logic from semantic.py and async_search.py.

Wraps IndexingPipeline + FileReaderProtocol into a single async-first service
for document indexing.  All DB access uses the sync session_factory (via
FileReaderProtocol.get_session or the injected session_factory) so that the
service works identically with SQLite and PostgreSQL back-ends.

Key design decisions:
  - Decision 14A: content-hash skip logic avoids redundant embedding API calls
  - Immutable inputs: never mutates constructor arguments
  - Keyword-only __init__ for clarity at call sites
  - Comprehensive error handling with structured logging
"""

import hashlib
import logging
import posixpath
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.virtual_views import is_parseable_path

# Removed: txtai handles this (Issue #2663)
# from nexus.bricks.search.embeddings import EmbeddingProvider
# from nexus.bricks.search.vector_db import VectorDatabase
try:
    from nexus.bricks.search.embeddings import EmbeddingProvider
except ImportError:
    EmbeddingProvider = Any
try:
    from nexus.bricks.search.vector_db import VectorDatabase
except ImportError:
    VectorDatabase = Any

from nexus.bricks.search.indexing import IndexingPipeline, IndexResult
from nexus.bricks.search.models import DocumentChunkModel, FilePathModel
from nexus.bricks.search.protocols import FileReaderProtocol

logger = logging.getLogger(__name__)


def _looks_like_virtual_readme(path: str) -> bool:
    """Heuristic: does ``path`` match a virtual ``.readme/`` overlay entry?

    Issue #3728: the virtual ``.readme/`` tree has no metastore rows.
    When ``_query_file_model`` misses on a path under ``.readme/``, this
    helper lets the indexer treat it as a virtual-doc candidate and
    synthesize a path_id instead of silently dropping the file.

    The check is intentionally path-shape-only — the router /
    backend introspection that would give us definitive "is this
    virtual?" answers isn't available from ``IndexingService``
    (which only holds a ``FileReaderProtocol``).  False positives
    are bounded: a user-created real ``.readme/`` folder whose row
    happens to be missing would index with a synthetic ``virtual:``
    path_id which doesn't collide with real path_ids.
    """
    normalized = posixpath.normpath(path)
    segments = normalized.split("/")
    return any(seg == ".readme" for seg in segments)


def _virtual_path_id(path: str) -> str:
    """Deterministic synthetic path_id for a virtual readme path.

    ``document_chunks.path_id`` is a 36-character FK to ``file_paths``, so
    row-less virtual documents still need a UUID-shaped identifier. UUID5
    keeps the value stable across runs so re-indexing replaces the previous
    chunks via the pipeline's path_id-keyed upsert.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nexus:virtual-readme:{path}"))


# Binary extensions excluded from directory indexing.
#
# Parseable binaries (.pdf, .docx, .xlsx, …) intentionally stay indexable: the
# file-reader adapter (``_NexusFSFileReader.read_text``) decodes them through
# the parsers brick so index_directory sees markdown text instead of raw bytes.
_BINARY_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".zip",
    ".tar",
    ".gz",
    ".exe",
    ".bin",
)


class IndexingService:
    """Async-first service that consolidates document indexing logic.

    Delegates chunking / embedding / bulk-insert to ``IndexingPipeline`` while
    owning the content-hash lifecycle, file reading, and index bookkeeping.
    """

    def __init__(
        self,
        *,
        pipeline: IndexingPipeline,
        file_reader: FileReaderProtocol,
        session_factory: Any,
        vector_db: VectorDatabase,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._file_reader = file_reader
        self._session_factory = session_factory
        self._vector_db = vector_db
        self._embedding_provider = embedding_provider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def index_document(self, path: str, *, force: bool = False) -> int:
        """Index (or re-index) a single document.

        Reads content via *file_reader*, checks the content-hash to decide
        whether re-indexing is necessary (decision 14A), deletes stale chunks,
        delegates to the pipeline, and updates tracking fields on success.

        Args:
            path: Virtual path of the document.
            force: If ``True``, bypass the content-hash skip check.

        Returns:
            Number of chunks indexed (``0`` when skipped or empty).

        Raises:
            ValueError: If the path does not exist in the database.
        """
        # --- Step 1: Resolve path_id and evaluate skip logic ---------------
        with self._get_session() as session:
            file_model = self._query_file_model(session, path)
            if file_model is None:
                raise ValueError(f"File not found in database: {path}")

            path_id: str = file_model.path_id
            current_content_id: str | None = file_model.content_id
            observed_indexed_hash: str | None = file_model.indexed_content_id

            if (
                not force
                and current_content_id is not None
                and observed_indexed_hash == current_content_id
            ):
                # Content unchanged -- return existing chunk count.
                existing = self._count_chunks(session, path_id)
                logger.debug(
                    "[INDEXING-SVC] Skipping unchanged document %s (%d chunks)",
                    path,
                    existing,
                )
                return existing

        # --- Step 2: Read document content ---------------------------------
        content = await self._read_content(path)

        # Parseable binaries (.pdf, .docx, .xlsx, …) surface an empty string
        # from the reader in TWO distinct scenarios which need different
        # handling:
        #   * parse error — parser missing, raised, or returned ``None``.
        #     Advancing ``indexed_content_id`` would latch the failure
        #     (the next reindex sees the hash match and skips forever),
        #     so leave tracking fields untouched and retry next tick.
        #   * successful empty parse — image-only PDF, blank .docx, scanned
        #     doc awaiting OCR.  The parser legitimately produced zero
        #     searchable text.  Retrying forever is wasted work and the
        #     stale-chunk delete would wipe chunks that SHOULD stay gone
        #     for this revision.  Advance ``indexed_content_id`` and move
        #     on — the file is genuinely "no text to index."
        #
        # The adapter distinguishes the two by writing ``parsed_text_hash``
        # only on successful parses (including empty ones).  Ask it via
        # ``has_successful_parse`` before picking the retry path.
        if is_parseable_path(path) and not (content and content.strip()):
            parse_ok = False
            if current_content_id is not None:
                parse_ok_check = getattr(self._file_reader, "has_successful_parse", None)
                if callable(parse_ok_check):
                    try:
                        parse_ok = bool(parse_ok_check(path, current_content_id))
                    except Exception:
                        parse_ok = False

            if parse_ok:
                # Successful parse, zero text — atomically replace any
                # prior chunks with nothing: delete stale rows for this
                # path_id AND advance ``indexed_content_id`` in the same
                # transaction.  Without the DELETE, a non-empty previous
                # revision's chunks would remain live even though the
                # current content legitimately has no text — search would
                # keep serving the stale version forever since the normal
                # hash-match skip path will never revisit this row.
                #
                # Use ``SELECT … FOR UPDATE`` on ``file_paths`` so a
                # concurrent reindex that lands between our step-1 read
                # and this block serializes behind us (same CAS discipline
                # as the parse-error stale-chunk branch below).
                with self._get_session() as session:
                    try:
                        locked = session.execute(
                            select(FilePathModel)
                            .where(
                                FilePathModel.path_id == path_id,
                                FilePathModel.deleted_at.is_(None),
                            )
                            .with_for_update()
                        ).scalar_one_or_none()
                    except Exception:
                        locked = session.execute(
                            select(FilePathModel).where(
                                FilePathModel.path_id == path_id,
                                FilePathModel.deleted_at.is_(None),
                            )
                        ).scalar_one_or_none()
                    # CAS: only apply the zero-chunk replacement when the
                    # row still holds the same indexed_content_id we
                    # observed in step 1.  If a concurrent writer has
                    # already advanced past us, let them win — their
                    # indexing was more recent than ours.
                    if locked is not None and locked.indexed_content_id == observed_indexed_hash:
                        session.execute(
                            delete(DocumentChunkModel).where(
                                DocumentChunkModel.path_id == path_id,
                            ),
                        )
                        locked.indexed_content_id = current_content_id
                        locked.last_indexed_at = datetime.now(UTC)
                        session.commit()
                        logger.info(
                            "[INDEXING-SVC] Successful empty parse for %s — cleared "
                            "prior chunks and advanced indexed_content_id",
                            path,
                        )
                    else:
                        logger.info(
                            "[INDEXING-SVC] Skipped empty-parse replace for %s — "
                            "indexed_content_id advanced under us (CAS miss)",
                            path,
                        )
                return 0
            if (
                current_content_id is not None
                and observed_indexed_hash is not None
                and observed_indexed_hash != current_content_id
            ):
                # Atomic CAS: lock the file_paths row (``SELECT … FOR UPDATE``)
                # before re-reading ``indexed_content_id`` so a concurrent
                # reindex that successfully advanced the hash between step 1
                # and now serializes behind us — our transaction either
                # observes the pre-advance value and deletes, or observes the
                # post-advance value and aborts.  Without the row lock, a
                # concurrent worker could complete its upsert in the gap
                # between the recheck SELECT and the DELETE and we'd still
                # wipe its fresh chunks (plain-SELECT snapshot doesn't block
                # other writers).
                #
                # ``.with_for_update()`` is a no-op under SQLite (single
                # writer anyway) and takes a row-level lock on Postgres.
                with self._get_session() as session:
                    try:
                        locked = session.execute(
                            select(FilePathModel)
                            .where(
                                FilePathModel.path_id == path_id,
                                FilePathModel.deleted_at.is_(None),
                            )
                            .with_for_update()
                        ).scalar_one_or_none()
                    except Exception:
                        # Some back-ends (e.g. SQLite in autocommit) can
                        # refuse FOR UPDATE — retry without the lock clause
                        # so the stale cleanup still runs.  The CAS re-read
                        # still protects against the non-concurrent case.
                        locked = session.execute(
                            select(FilePathModel).where(
                                FilePathModel.path_id == path_id,
                                FilePathModel.deleted_at.is_(None),
                            )
                        ).scalar_one_or_none()
                    if locked is not None and locked.indexed_content_id == observed_indexed_hash:
                        session.execute(
                            delete(DocumentChunkModel).where(
                                DocumentChunkModel.path_id == path_id,
                            ),
                        )
                        session.commit()
                        logger.warning(
                            "[INDEXING-SVC] Parse failed for changed %s — cleared "
                            "stale chunks, will retry on next tick",
                            path,
                        )
                    else:
                        logger.info(
                            "[INDEXING-SVC] Skipped stale-chunk delete for %s — "
                            "indexed_content_id advanced under us (CAS miss)",
                            path,
                        )
            else:
                logger.warning(
                    "[INDEXING-SVC] Empty content for parseable binary %s — "
                    "skipping index tick, will retry next run",
                    path,
                )
            return 0

        # --- Step 3: Delegate to pipeline (atomic delete+insert) -----------
        # The pipeline's _bulk_insert handles DELETE old chunks + INSERT new
        # chunks in a single transaction.  We do NOT delete chunks beforehand
        # because a pipeline failure (e.g. embedding API timeout) would leave
        # the document with zero chunks — an incomplete index (Issue #2753).
        try:
            result: IndexResult = await self._pipeline.index_document(
                path,
                content,
                path_id,
            )
        except Exception:
            logger.exception("[INDEXING-SVC] Pipeline failed for %s", path)
            raise

        chunks_indexed = result.chunks_indexed

        # --- Step 5: Update tracking fields --------------------------------
        with self._get_session() as session:
            file_model = session.get(FilePathModel, path_id)
            if file_model is not None:
                file_model.indexed_content_id = current_content_id
                file_model.last_indexed_at = datetime.now(UTC)
                session.commit()

        logger.info(
            "[INDEXING-SVC] Indexed %s: %d chunks",
            path,
            chunks_indexed,
        )
        return chunks_indexed

    async def index_directory(
        self,
        path: str = "/",
    ) -> dict[str, IndexResult]:
        """Index every indexable file under *path*.

        Lists files via *file_reader*, filters out binary extensions, reads
        content, resolves path IDs, and delegates to the pipeline for parallel
        indexing.

        Args:
            path: Root directory to index (default ``"/"`` for all files).

        Returns:
            Mapping of virtual path to ``IndexResult``.
        """
        files_result = await self._file_reader.list_files(path, recursive=True)
        files = files_result.items if hasattr(files_result, "items") else files_result

        # Build (path, content, path_id) tuples, skipping binary files.
        documents: list[tuple[str, str, str]] = []

        with self._get_session() as session:
            for entry in files:
                file_path = entry if isinstance(entry, str) else entry.get("name", "")
                if not file_path or file_path.endswith("/"):
                    continue
                if file_path.endswith(_BINARY_EXTENSIONS):
                    continue

                try:
                    content = await self._read_content(file_path)
                    file_model = self._query_file_model(session, file_path)
                    if file_model is not None:
                        documents.append(
                            (file_path, content, file_model.path_id),
                        )
                    elif _looks_like_virtual_readme(file_path):
                        # Issue #3728: virtual ``.readme/`` overlay paths
                        # are served from class metadata at read time.  The
                        # chunk tables still enforce a FK to file_paths, so
                        # persist a minimal deterministic row before handing
                        # the document to the indexing pipeline.
                        #
                        # Known limitation: when the connector class
                        # metadata changes (e.g. a nexus upgrade), the
                        # synthetic path_id stays the same but the
                        # embeddings become stale.  Users need to
                        # re-trigger indexing (remount) to refresh.
                        virtual_model = self._ensure_virtual_readme_file_model(
                            session,
                            file_path,
                            content,
                        )
                        documents.append((file_path, content, virtual_model.path_id))
                except Exception:
                    logger.warning(
                        "[INDEXING-SVC] Skipping %s: failed to read or resolve",
                        file_path,
                        exc_info=True,
                    )

        if not documents:
            return {}

        results = await self._pipeline.index_documents(documents)
        return {r.path: r for r in results}

    async def delete_document_index(self, path: str) -> None:
        """Remove all indexed chunks for the document at *path*.

        Args:
            path: Virtual path of the document.
        """
        with self._get_session() as session:
            file_model = self._query_file_model(session, path)
            if file_model is None:
                return  # Nothing to delete.

            session.execute(
                delete(DocumentChunkModel).where(
                    DocumentChunkModel.path_id == file_model.path_id,
                ),
            )
            session.commit()

        logger.info("[INDEXING-SVC] Deleted index for %s", path)

    async def get_index_stats(self) -> dict[str, Any]:
        """Return aggregate indexing statistics.

        Returns:
            Dictionary containing ``total_chunks``, ``indexed_files``,
            ``embedding_provider``, and ``vector_db`` diagnostics.
        """
        with self._get_session() as session:
            total_chunks: int = (
                session.execute(
                    select(func.count()).select_from(DocumentChunkModel),
                ).scalar()
                or 0
            )

            indexed_files: int = (
                session.execute(
                    select(
                        func.count(func.distinct(DocumentChunkModel.path_id)),
                    ),
                ).scalar()
                or 0
            )

        return {
            "total_chunks": total_chunks,
            "indexed_files": indexed_files,
            "embedding_provider": (
                self._embedding_provider.__class__.__name__
                if self._embedding_provider is not None
                else None
            ),
            "vector_db": self._vector_db.get_stats(),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_session(self) -> Any:
        """Return a context-manager that yields a DB session."""
        return self._file_reader.get_session()

    async def _read_content(self, path: str) -> str:
        """Read document content, preferring pre-processed searchable text."""
        content = self._file_reader.get_searchable_text(path)
        if content is None:
            content = await self._file_reader.read_text(path)
        return content

    @staticmethod
    def _query_file_model(
        session: Any,
        path: str,
    ) -> Any:
        """Query a single ``FilePathModel`` by virtual path (non-deleted)."""
        stmt = select(FilePathModel).where(
            FilePathModel.virtual_path == path,
            FilePathModel.deleted_at.is_(None),
        )
        return session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def _count_chunks(session: Any, path_id: str) -> int:
        """Return the number of chunks for *path_id*."""
        stmt = (
            select(func.count())
            .select_from(DocumentChunkModel)
            .where(DocumentChunkModel.path_id == path_id)
        )
        return session.execute(stmt).scalar() or 0

    @staticmethod
    def _ensure_virtual_readme_file_model(
        session: Any,
        path: str,
        content: str,
    ) -> Any:
        """Create or refresh the synthetic file_paths row for a virtual readme."""
        path_id = _virtual_path_id(path)
        content_id = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)

        file_model = session.get(FilePathModel, path_id)
        if file_model is None:
            file_model = session.execute(
                select(FilePathModel).where(
                    FilePathModel.zone_id == ROOT_ZONE_ID,
                    FilePathModel.virtual_path == path,
                    FilePathModel.deleted_at.is_(None),
                )
            ).scalar_one_or_none()

        if file_model is None:
            file_model = FilePathModel(
                path_id=path_id,
                zone_id=ROOT_ZONE_ID,
                virtual_path=path,
                file_type="text/markdown",
                size_bytes=len(content.encode("utf-8")),
                content_id=content_id,
                created_at=now,
                updated_at=now,
            )
            session.add(file_model)
        else:
            file_model.content_id = content_id
            file_model.size_bytes = len(content.encode("utf-8"))
            file_model.file_type = file_model.file_type or "text/markdown"
            file_model.updated_at = now
            file_model.deleted_at = None

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            file_model = session.get(FilePathModel, path_id)
            if file_model is None:
                file_model = session.execute(
                    select(FilePathModel).where(
                        FilePathModel.zone_id == ROOT_ZONE_ID,
                        FilePathModel.virtual_path == path,
                        FilePathModel.deleted_at.is_(None),
                    )
                ).scalar_one_or_none()
            if file_model is None:
                raise

        return file_model

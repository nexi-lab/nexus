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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select

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

    Prefixed with ``virtual:`` so it can't collide with a real
    FilePathModel path_id.  SHA-256 of the virtual path keeps it
    stable across runs so re-indexing deletes the previous chunks
    via the pipeline's path_id-keyed upsert.
    """
    return "virtual:" + hashlib.sha256(path.encode("utf-8")).hexdigest()


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
            current_content_hash: str | None = file_model.content_hash

            if (
                not force
                and current_content_hash is not None
                and file_model.indexed_content_hash == current_content_hash
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
                file_model.indexed_content_hash = current_content_hash
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
                        # are row-less by design — the tree is served from
                        # class metadata at read time so there's nothing
                        # to persist.  Use a deterministic synthetic
                        # path_id so the search pipeline can still chunk
                        # + embed + store the content.  The ``virtual:``
                        # prefix namespaces them away from real FilePathModel
                        # path_ids so they can't collide.
                        #
                        # Known limitation: when the connector class
                        # metadata changes (e.g. a nexus upgrade), the
                        # synthetic path_id stays the same but the
                        # embeddings become stale.  Users need to
                        # re-trigger indexing (remount) to refresh.
                        synth_path_id = _virtual_path_id(file_path)
                        documents.append((file_path, content, synth_path_id))
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

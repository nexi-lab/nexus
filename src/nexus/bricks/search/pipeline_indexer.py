"""PipelineIndexer — standalone RPC-path indexer (Issue #2075).

Extracted from ``SemanticSearchMixin._pipeline_index_documents``.

Used when semantic search is initialized via RPC (without a NexusFS instance),
so IndexingService cannot be created.  PipelineIndexer delegates to
``IndexingPipeline`` for chunking + embedding and looks up path_ids directly
in the database.

Key improvements over the inlined version:
- DI constructor (no mixin self-references)
- Batch ``get_searchable_text_bulk`` when the metastore supports it (13A)
- Single DB query for all path_ids instead of N individual queries
"""

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.storage.models import FilePathModel

if TYPE_CHECKING:
    from nexus.bricks.search.indexing import IndexingPipeline

logger = logging.getLogger(__name__)


class PipelineIndexer:
    """Indexes documents via IndexingPipeline for the RPC path.

    Unlike ``IndexingService`` (which requires a ``FileReaderProtocol``),
    this class works with raw callables for reading and listing, making it
    suitable for the RPC init path where NexusFS is unavailable.
    """

    def __init__(
        self,
        *,
        pipeline: "IndexingPipeline",
        session_factory: Callable[..., Any],
        metadata: Any,
        file_reader: Callable[[str], bytes],
        file_lister: Callable[[str, bool], Any],
    ) -> None:
        self._pipeline = pipeline
        self._session_factory = session_factory
        self._metadata = metadata
        self._file_reader = file_reader
        self._file_lister = file_lister

    async def index_path(
        self,
        path: str,
        recursive: bool = True,
    ) -> dict[str, int]:
        """Index a file or directory.

        Args:
            path: Virtual path to index (file or directory).
            recursive: Whether to recurse into subdirectories.

        Returns:
            Mapping of ``{path: chunks_indexed}``.
        """
        files_to_index = await self._resolve_files(path, recursive)
        if not files_to_index:
            return {}

        documents = await asyncio.to_thread(self._prepare_documents, files_to_index)
        if not documents:
            return {}

        idx_results = await self._pipeline.index_documents(documents)
        return {r.path: r.chunks_indexed for r in idx_results}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _resolve_files(self, path: str, recursive: bool) -> list[str]:
        """Determine which files to index."""
        try:
            await asyncio.to_thread(self._file_reader, path)
            return [path]
        except Exception:
            file_list = await asyncio.to_thread(self._file_lister, path, recursive)
            if hasattr(file_list, "items"):
                file_list = file_list.items
            return [
                (item if isinstance(item, str) else item.get("path", ""))
                for item in file_list
                if (item if isinstance(item, str) else item.get("path", ""))
                and not (item if isinstance(item, str) else item.get("path", "")).endswith("/")
            ]

    def _prepare_documents(self, files: list[str]) -> list[tuple[str, str, str]]:
        """Read content and look up path_ids for all files.

        Uses batch metastore lookup when available, and a single DB query
        for path_ids (instead of N individual queries).
        """
        # --- Batch text lookup ---
        text_map: dict[str, str] = {}
        if hasattr(self._metadata, "get_searchable_text_bulk"):
            text_map = self._metadata.get_searchable_text_bulk(files)
        else:
            for fp in files:
                text = self._metadata.get_searchable_text(fp)
                if text is not None:
                    text_map[fp] = text

        # Fall back to raw read for files without searchable text
        for fp in files:
            if fp not in text_map:
                try:
                    raw = self._file_reader(fp)
                    if isinstance(raw, bytes):
                        text_map[fp] = raw.decode("utf-8", errors="ignore")
                    else:
                        text_map[fp] = str(raw)
                except Exception as exc:
                    logger.warning("Failed to read %s for indexing: %s", fp, exc)

        if not text_map:
            return []

        # --- Batch path_id lookup (single DB query) ---
        path_id_map: dict[str, str] = {}
        paths_with_content = list(text_map.keys())
        with self._session_factory() as session:
            stmt = select(FilePathModel.virtual_path, FilePathModel.path_id).where(
                FilePathModel.virtual_path.in_(paths_with_content),
                FilePathModel.deleted_at.is_(None),
            )
            for row in session.execute(stmt):
                path_id_map[row.virtual_path] = row.path_id

        return [
            (fp, text_map[fp], path_id_map[fp])
            for fp in paths_with_content
            if fp in path_id_map and text_map[fp]
        ]

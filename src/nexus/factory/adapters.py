"""Factory adapters — NexusFSFileReader, DaemonSkeletonBM25, WorkflowLifecycleAdapter."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from nexus.lib.virtual_views import PARSEABLE_EXTENSIONS

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
        raw_bytes = (
            content_raw
            if isinstance(content_raw, bytes)
            else str(content_raw).encode("utf-8", errors="ignore")
        )

        # Parseable binaries (.pdf/.docx/.xlsx/…) — decode via parse_fn so the
        # search index gets real text, not utf-8 garbage from raw bytes.  The
        # metastore parsed_text cache, populated by ContentParserEngine /
        # AutoParseWriteHook, is checked first to avoid re-parsing.
        #
        # Fail-closed: if the parser is missing or errors out, we return an
        # empty string instead of the raw-byte decoding.  Shipping utf-8 soup
        # into the embedding pipeline wastes API budget and pollutes results;
        # an empty content makes the daemon skip indexing and fall through to
        # its content_cache probe.
        if any(path.endswith(ext) for ext in PARSEABLE_EXTENSIONS):
            parsed = await self._get_parsed_text(path, raw_bytes)
            return _sanitize_for_index(parsed) if parsed is not None else ""

        if isinstance(content_raw, bytes):
            return _sanitize_for_index(content_raw.decode("utf-8", errors="ignore"))
        return _sanitize_for_index(str(content_raw))

    async def _get_parsed_text(self, path: str, raw: bytes) -> str | None:
        """Return cached or freshly-parsed markdown for a parseable binary.

        Returns None when no parser is wired or parsing fails — caller maps
        this to an empty ``read_text`` result so the indexing pipeline skips
        the file instead of embedding raw bytes.
        """
        # 1) Metastore cache (shared with VirtualViewResolver / ContentParserEngine).
        #    Defense in depth: sanitize on read in case an older entry was cached
        #    before the write-path sanitizer existed.
        try:
            cached = self._nx.metadata.get_file_metadata(path, "parsed_text")
            if cached:
                cached_str = (
                    cached if isinstance(cached, str) else cached.decode("utf-8", errors="ignore")
                )
                return _sanitize_for_index(cached_str)
        except Exception:
            pass

        # 2) Synchronous parse via injected parse_fn.  The callable is sync, so
        #    run it in a worker thread — otherwise a slow PDF blocks the event
        #    loop and pins unrelated search/RPC traffic while it parses.
        if self._parse_fn is None:
            logger.warning(
                "read_text: no parse_fn wired for parseable binary %s — "
                "skipping instead of indexing raw bytes",
                path,
            )
            return None
        try:
            parsed_bytes = await asyncio.to_thread(self._parse_fn, raw, path)
        except Exception:
            logger.warning("read_text: parse_fn raised for %s", path, exc_info=True)
            return None
        if parsed_bytes is None:
            logger.warning("read_text: parse_fn returned None for %s", path)
            return None
        text = _sanitize_for_index(parsed_bytes.decode("utf-8", errors="ignore"))
        logger.info("read_text: parsed %s → %d chars markdown", path, len(text))

        # 3) Populate the metastore cache with the sanitized text so subsequent
        #    reads (virtual view resolver, next index refresh) never see NULs.
        try:
            from datetime import UTC, datetime

            self._nx.metadata.set_file_metadata(path, "parsed_text", text)
            self._nx.metadata.set_file_metadata(path, "parsed_at", datetime.now(UTC).isoformat())
        except Exception:
            pass  # best-effort — search result correctness doesn't need the cache.

        return text

    def get_searchable_text(self, path: str) -> str | None:
        result: str | None = self._nx.metadata.get_searchable_text(path)
        return result

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

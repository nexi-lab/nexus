"""Factory adapters — NexusFSFileReader, DaemonSkeletonBM25, WorkflowLifecycleAdapter."""

from collections.abc import Callable
from typing import Any

from nexus.lib.virtual_views import PARSEABLE_EXTENSIONS

# =========================================================================
# Issue #1520: NexusFS → FileReaderProtocol adapter
# =========================================================================


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
        if any(path.endswith(ext) for ext in PARSEABLE_EXTENSIONS):
            parsed = self._get_parsed_text(path, raw_bytes)
            if parsed is not None:
                return parsed

        if isinstance(content_raw, bytes):
            return content_raw.decode("utf-8", errors="ignore")
        return str(content_raw)

    def _get_parsed_text(self, path: str, raw: bytes) -> str | None:
        """Return cached or freshly-parsed markdown for a parseable binary.

        Returns None when no parser is wired or parsing fails; caller falls
        back to raw-byte decoding so search still sees *something*.
        """
        # 1) Metastore cache (shared with VirtualViewResolver / ContentParserEngine).
        try:
            cached = self._nx.metadata.get_file_metadata(path, "parsed_text")
            if cached:
                return (
                    cached if isinstance(cached, str) else cached.decode("utf-8", errors="ignore")
                )
        except Exception:
            pass

        # 2) Synchronous parse via injected parse_fn. parse_fn is the same
        #    callable used by the VFS virtual-view resolver.
        if self._parse_fn is None:
            return None
        try:
            parsed_bytes = self._parse_fn(raw, path)
        except Exception:
            return None
        if parsed_bytes is None:
            return None
        text = parsed_bytes.decode("utf-8", errors="ignore")

        # 3) Populate the metastore cache so subsequent read_text / virtual-view
        #    reads are free.
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

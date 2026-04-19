"""sqlite-vec search backend for SANDBOX profile (Issue #3778).

Uses the ``sqlite-vec`` extension for vector storage + KNN; ``litellm``
for remote embeddings (BYO API key — any provider). Keeps zone isolation
via a ``zone_id`` column on the ``vec0`` virtual table.

Design notes
------------
* sqlite3 is sync; every DB call is wrapped in ``asyncio.to_thread`` so
  the event loop stays responsive. We deliberately do *not* use
  ``aiosqlite`` here because sqlite-vec must be loaded as an extension
  via ``conn.enable_load_extension(True)`` + ``sqlite_vec.load(conn)``,
  and that path is documented and well-tested on the sync ``sqlite3``
  driver.
* sqlite-vec accepts embedding bytes packed as little-endian float32
  (``struct.pack(f"{dim}f", *vec)``). We pack inside the worker thread
  so the calling coroutine doesn't block.
* Zone isolation is enforced in WHERE-clause SQL (``zone_id = :zid``).
  This matches the txtai backend's contract.
* Stable IDs: ``(zone_id, path, chunk_index)`` — recomputing the rowid
  from a stable hash lets ``upsert`` replace existing rows
  deterministically.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import struct
from typing import Any

from nexus.bricks.search.results import BaseSearchResult

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536  # text-embedding-3-small native dim
_VEC_TABLE = "nexus_vec"


class SqliteVecBackend:
    """Vector search backend using sqlite-vec + litellm embeddings.

    Designed for SANDBOX profile: zero external services (just an
    embedding API key). Used as the primary semantic path on SANDBOX;
    callers fall back to the existing federation-then-BM25S chain when
    this backend is unavailable or returns nothing.

    The ``vec0`` virtual table stores: ``embedding`` (float[dim]),
    ``zone_id`` (text), ``path`` (text), ``chunk_text`` (text),
    ``chunk_index`` (integer). KNN queries are filtered by ``zone_id``.
    """

    def __init__(
        self,
        *,
        db_path: str,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        api_key: str | None = None,
    ) -> None:
        # Import-time check so callers see a clear error before they try
        # to startup() the backend. The factory swallows ImportError and
        # logs a WARNING naming the missing package.
        try:
            import sqlite_vec  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "SqliteVecBackend requires the 'sqlite-vec' package. "
                "Install with: pip install 'nexus-ai-fs[sandbox]'"
            ) from exc
        try:
            import litellm  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "SqliteVecBackend requires the 'litellm' package. "
                "Install with: pip install 'nexus-ai-fs[sandbox]'"
            ) from exc

        self._db_path = db_path
        self._embedding_model = embedding_model or os.environ.get(
            "NEXUS_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        )
        self._embedding_dim = int(embedding_dim or DEFAULT_EMBEDDING_DIM)
        self._api_key = api_key  # optional; litellm reads env by default
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        # R1 review: serialize DB ops on the shared connection. sqlite3
        # Connection created with ``check_same_thread=False`` is not safe
        # against concurrent use from multiple threadpool workers; we wrap
        # every data op in this lock to prevent interleaving. Separate from
        # ``_lock`` (startup/shutdown) to avoid deadlock on shutdown.
        self._op_lock = asyncio.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def startup(self) -> None:
        """Open the SQLite connection, load sqlite-vec, ensure the table.

        Idempotent: a second call is a no-op.
        """
        async with self._lock:
            if self._started:
                return

            def _open() -> sqlite3.Connection:
                # check_same_thread=False because we hop through to_thread
                # and the executor's worker may differ between calls.
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                conn.enable_load_extension(True)
                import sqlite_vec

                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                # Create vec0 virtual table; embedding dim is fixed at
                # init time (sqlite-vec requirement). Auxiliary columns
                # carry zone isolation + result rendering data.
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE} USING vec0("
                    f"embedding float[{self._embedding_dim}], "
                    f"zone_id text, "
                    f"path text, "
                    f"chunk_text text, "
                    f"chunk_index integer"
                    f");"
                )
                conn.commit()
                return conn

            self._conn = await asyncio.to_thread(_open)
            self._started = True
            logger.info(
                "[SqliteVecBackend] started (db=%s model=%s dim=%d)",
                self._db_path,
                self._embedding_model,
                self._embedding_dim,
            )

    async def shutdown(self) -> None:
        """Close the SQLite connection."""
        async with self._lock:
            if self._conn is not None:
                conn = self._conn
                self._conn = None
                self._started = False
                await asyncio.to_thread(conn.close)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _stable_rowid(zone_id: str, path: str, chunk_index: int) -> int:
        """Derive a stable signed-64-bit integer rowid for the row key.

        sqlite-vec uses the rowid as the primary key on vec0 tables, so
        we need a deterministic ID per ``(zone_id, path, chunk_index)``
        tuple to enable idempotent upsert (delete-then-insert).
        """
        h = hashlib.blake2b(
            f"{zone_id}\x00{path}\x00{chunk_index}".encode(),
            digest_size=8,
        ).digest()
        # Convert to signed 64-bit int (sqlite rowid range).
        n = int.from_bytes(h, "big", signed=False)
        # Map to range (0, 2**63-1] — non-negative so the value is
        # legal for sqlite rowid (negatives are also legal, but staying
        # positive avoids signed/unsigned confusion in tests).
        return (n & 0x7FFFFFFFFFFFFFFF) or 1

    @staticmethod
    def _pack_vector(vec: list[float] | tuple[float, ...]) -> bytes:
        """Pack a float vector into little-endian float32 bytes.

        sqlite-vec accepts bytes (packed float32) directly as the value
        for a ``float[N]`` column.
        """
        return struct.pack(f"{len(vec)}f", *vec)

    async def _embed_one(self, text: str) -> list[float]:
        """Embed a single string via litellm.aembedding."""
        import litellm

        kwargs: dict[str, Any] = {"model": self._embedding_model, "input": [text]}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        resp = await litellm.aembedding(**kwargs)
        # resp.data is a list of {"embedding": [...], "index": ...} dicts.
        vec = resp.data[0]["embedding"]
        return list(vec)

    async def _embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings. Falls back to per-item on TypeError."""
        if not texts:
            return []
        import litellm

        kwargs: dict[str, Any] = {"model": self._embedding_model, "input": texts}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        resp = await litellm.aembedding(**kwargs)
        return [list(item["embedding"]) for item in resp.data]

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError(
                "SqliteVecBackend is not started. Call await backend.startup() first."
            )
        return self._conn

    # ------------------------------------------------------------------
    # SearchBackendProtocol surface
    # ------------------------------------------------------------------
    async def upsert(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Embed each document's text and insert / replace into the vec0 table.

        Each document dict must carry at least ``path`` and ``text``. An
        optional ``chunk_index`` (default 0) lets callers store multiple
        chunks per file. The row's identity is
        ``(zone_id, path, chunk_index)``.
        """
        if not documents:
            return 0
        await self.startup()
        conn = self._require_conn()

        texts = [str(doc.get("text") or doc.get("chunk_text") or "") for doc in documents]
        vectors = await self._embed_many(texts)
        if len(vectors) != len(documents):
            raise RuntimeError(
                f"Embedding count mismatch: got {len(vectors)} vectors for "
                f"{len(documents)} documents (model={self._embedding_model})"
            )

        rows: list[tuple[int, bytes, str, str, str, int]] = []
        for doc, vec in zip(documents, vectors, strict=True):
            path = str(doc.get("path", ""))
            chunk_index = int(doc.get("chunk_index", 0) or 0)
            chunk_text = str(doc.get("text") or doc.get("chunk_text") or "")
            if len(vec) != self._embedding_dim:
                raise RuntimeError(
                    f"Embedding dim mismatch: got {len(vec)} expected "
                    f"{self._embedding_dim} (model={self._embedding_model}, path={path})"
                )
            rowid = self._stable_rowid(zone_id, path, chunk_index)
            rows.append((rowid, self._pack_vector(vec), zone_id, path, chunk_text, chunk_index))

        def _write() -> int:
            # vec0 doesn't support UPSERT; emulate by deleting any row with
            # the same rowid before inserting.
            ids = [r[0] for r in rows]
            placeholders = ",".join("?" for _ in ids)
            with conn:
                conn.execute(
                    f"DELETE FROM {_VEC_TABLE} WHERE rowid IN ({placeholders})",
                    ids,
                )
                conn.executemany(
                    f"INSERT INTO {_VEC_TABLE}"
                    f"(rowid, embedding, zone_id, path, chunk_text, chunk_index) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            return len(rows)

        async with self._op_lock:
            return await asyncio.to_thread(_write)

    async def index(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Full-rebuild for *zone_id*: drop the zone's rows, then upsert all.

        R5 review (Issue #3778): compute embeddings BEFORE the destructive
        wipe, then do the delete + insert atomically in a single SQL
        transaction. If the remote embedding API fails mid-flight, the
        existing zone index remains intact instead of being left empty.
        """
        if not documents:
            return 0
        await self.startup()
        conn = self._require_conn()

        # Phase 1: embed outside the lock / transaction. If this raises,
        # the existing zone index is unaffected.
        texts = [str(doc.get("text") or doc.get("chunk_text") or "") for doc in documents]
        vectors = await self._embed_many(texts)
        if len(vectors) != len(documents):
            raise RuntimeError(
                f"Embedding count mismatch: got {len(vectors)} vectors for "
                f"{len(documents)} documents (model={self._embedding_model})"
            )

        rows: list[tuple[int, bytes, str, str, str, int]] = []
        for doc, vec in zip(documents, vectors, strict=True):
            path = str(doc.get("path", ""))
            chunk_index = int(doc.get("chunk_index", 0) or 0)
            chunk_text = str(doc.get("text") or doc.get("chunk_text") or "")
            if len(vec) != self._embedding_dim:
                raise RuntimeError(
                    f"Embedding dim mismatch: got {len(vec)} expected "
                    f"{self._embedding_dim} (model={self._embedding_model}, path={path})"
                )
            rowid = self._stable_rowid(zone_id, path, chunk_index)
            rows.append((rowid, self._pack_vector(vec), zone_id, path, chunk_text, chunk_index))

        # Phase 2: atomic delete+insert in a single transaction. sqlite3's
        # `with conn:` uses BEGIN/COMMIT/ROLLBACK, so if executemany fails
        # the DELETE rolls back and the prior zone index is preserved.
        def _swap() -> int:
            with conn:
                conn.execute(
                    f"DELETE FROM {_VEC_TABLE} WHERE zone_id = ?",
                    (zone_id,),
                )
                conn.executemany(
                    f"INSERT INTO {_VEC_TABLE}"
                    f"(rowid, embedding, zone_id, path, chunk_text, chunk_index) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            return len(rows)

        async with self._op_lock:
            return await asyncio.to_thread(_swap)

    async def delete(self, ids: list[str], *, zone_id: str) -> int:
        """Delete rows by document id within *zone_id*.

        Each ``id`` is interpreted as a ``path`` — SANDBOX search keys
        on path within a zone, so this matches what callers expect.
        """
        if not ids:
            return 0
        await self.startup()
        conn = self._require_conn()

        def _delete() -> int:
            placeholders = ",".join("?" for _ in ids)
            with conn:
                cur = conn.execute(
                    f"DELETE FROM {_VEC_TABLE} WHERE zone_id = ? AND path IN ({placeholders})",
                    (zone_id, *ids),
                )
                return cur.rowcount or 0

        async with self._op_lock:
            return await asyncio.to_thread(_delete)

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        zone_id: str,
        search_type: str = "hybrid",  # noqa: ARG002 — caller fuses externally
        path_filter: str | None = None,
    ) -> list[BaseSearchResult]:
        """KNN search inside *zone_id*; returns top-K BaseSearchResult.

        ``search_type`` is accepted for protocol parity but ignored: the
        SqliteVecBackend only does pure vector KNN. Hybrid fusion is
        handled upstream by ``SearchService``.
        """
        if not query.strip():
            return []
        await self.startup()
        conn = self._require_conn()

        # Embed the query.
        try:
            qvec = await self._embed_one(query)
        except Exception as exc:
            logger.warning(
                "[SqliteVecBackend] query embedding failed (%s); returning empty results",
                exc,
            )
            return []
        if len(qvec) != self._embedding_dim:
            logger.warning(
                "[SqliteVecBackend] query embedding dim %d != table dim %d — skipping search",
                len(qvec),
                self._embedding_dim,
            )
            return []
        qbytes = self._pack_vector(qvec)

        # vec0 only allows EQUALS / comparison predicates on metadata
        # columns inside a KNN query — no LIKE / IN. To honour an optional
        # ``path_filter`` (prefix), we over-fetch with the equality-only
        # zone_id constraint and post-filter in Python. The over-fetch
        # factor is bounded so a tiny K doesn't blow up the workload.
        fetch_k = limit
        if path_filter:
            fetch_k = max(limit * 5, 50)

        def _query() -> list[tuple[Any, ...]]:
            sql = (
                f"SELECT rowid, zone_id, path, chunk_text, chunk_index, distance "
                f"FROM {_VEC_TABLE} "
                f"WHERE embedding MATCH ? AND zone_id = ? "
                f"ORDER BY distance LIMIT ?"
            )
            cur = conn.execute(sql, (qbytes, zone_id, fetch_k))
            return list(cur.fetchall())

        async with self._op_lock:
            rows = await asyncio.to_thread(_query)
        if path_filter:
            prefix = path_filter.rstrip("/")
            # ``prefix`` matches itself and any descendant path. Treat the
            # exact equal as a match too so a single-file filter still
            # returns the file.
            rows = [
                row
                for row in rows
                if str(row[2] or "") == prefix or str(row[2] or "").startswith(prefix + "/")
            ]
            rows = rows[:limit]

        results: list[BaseSearchResult] = []
        for _rowid, row_zone, path, chunk_text, chunk_index, distance in rows:
            # Convert distance -> similarity score in (0, 1]. sqlite-vec
            # returns L2 distance for default float[] columns; we map
            # via 1 / (1 + d) so smaller distance -> higher score.
            try:
                score = 1.0 / (1.0 + float(distance))
            except (TypeError, ValueError):
                score = 0.0
            results.append(
                BaseSearchResult(
                    path=str(path or ""),
                    chunk_text=str(chunk_text or ""),
                    score=score,
                    chunk_index=int(chunk_index or 0),
                    vector_score=score,
                    zone_id=str(row_zone or zone_id),
                )
            )
        return results

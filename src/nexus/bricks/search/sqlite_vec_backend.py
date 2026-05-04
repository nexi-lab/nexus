"""sqlite-vec search backend for SANDBOX profile (Issue #3778).

Uses the ``sqlite-vec`` extension for vector storage + KNN. Embeddings
come from one of two backends, picked at construct time:

* ``litellm`` — remote embeddings (BYO API key, any provider). Default
  model ``text-embedding-3-small`` (1536 dim).
* ``fastembed`` — local ONNX embeddings, zero network. Default model
  ``BAAI/bge-small-en-v1.5`` (384 dim). Used when no API key is
  available so SANDBOX keeps its "zero external services" promise even
  without a key.

Auto-selection (``embedder='auto'``, the default): pick ``fastembed``
when ``NEXUS_OFFLINE_EMBED`` is truthy or no embedding API key is in
the environment; otherwise pick ``litellm``.

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
import re
import sqlite3
import struct
import threading
import weakref
from typing import Any

from nexus.bricks.search.results import BaseSearchResult

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536  # text-embedding-3-small native dim
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_FASTEMBED_DIM = 384  # bge-small-en-v1.5 native dim
_VEC_TABLE = "nexus_vec"
# Codex review R3 (medium): companion table that stores the identity of
# the embedder that originally created the ``nexus_vec`` table. Enforces
# "only one embedder may share a DB" even when two backends happen to
# share the same vector dimension (e.g. two different 384-dim models —
# bge-small vs. all-MiniLM-L6 — would silently mix embedding spaces and
# corrupt KNN ranking otherwise). The dim check alone is insufficient.
_VEC_META_TABLE = "nexus_vec_meta"

# Env vars consulted to decide whether a remote embedding API is available.
# Order matches litellm's own provider precedence loosely.
_REMOTE_API_KEY_ENVS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "COHERE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_API_KEY",
    "VOYAGE_API_KEY",
    "MISTRAL_API_KEY",
    "NEXUS_EMBEDDING_API_KEY",
)


class SqliteVecDimMismatchError(RuntimeError):
    """Raised when an existing ``nexus_vec`` table was created with a
    different embedding dim than the backend now wants to use.

    sqlite-vec fixes the embedding dim at table creation time, so a
    backend configured for dim=384 cannot insert vectors into a table
    created with dim=1536. This commonly happens when a SANDBOX user
    initially populated the database with a remote (litellm) embedder,
    then later restarted without an API key and the auto-detect picks
    the local fastembed embedder. Surfacing the mismatch loudly is
    safer than silently failing every upsert.

    Resolution: either delete the existing ``nexus_vec`` table /
    database to rebuild with the new embedder, or pin the original
    embedder by setting an embedding API key (or
    ``NEXUS_EMBEDDER=litellm`` + ``NEXUS_EMBEDDING_MODEL``).
    """


class SqliteVecEmbedderMismatchError(RuntimeError):
    """Raised when an existing DB was populated by a *different*
    embedder than the one currently configured, even when the embedding
    dimension happens to match.

    Two different models (e.g. ``BAAI/bge-small-en-v1.5`` and
    ``sentence-transformers/all-MiniLM-L6-v2``) can both produce 384-d
    vectors but live in completely incompatible embedding spaces. Mixing
    their outputs in the same vec0 table silently corrupts KNN ranking
    — searches still return rows but the ordering becomes meaningless.

    The dim-check (``SqliteVecDimMismatchError``) cannot catch this
    case; the companion ``nexus_vec_meta`` table records the embedder
    identity at first start so subsequent opens can fail loudly instead
    of corrupting recall.

    Resolution: same as the dim case — delete the DB to rebuild with
    the new embedder, or pin the original embedder via env vars
    (``NEXUS_EMBEDDER`` / ``NEXUS_EMBEDDING_MODEL`` /
    ``NEXUS_OFFLINE_EMBED_MODEL``).
    """


_DIM_REGEX = re.compile(r"embedding\s+float\[(\d+)\]", re.IGNORECASE)


def _detect_embedder_kind(api_key: str | None) -> str:
    """Return ``"litellm"`` or ``"fastembed"`` based on env + arg.

    Rule:
      * ``NEXUS_OFFLINE_EMBED`` truthy → ``fastembed``
      * Explicit ``api_key`` arg present → ``litellm``
      * Any standard provider env var set → ``litellm``
      * Otherwise → ``fastembed`` (offline default — keeps SANDBOX's
        "zero external services" promise)
    """
    flag = (os.environ.get("NEXUS_OFFLINE_EMBED") or "").lower()
    if flag in ("1", "true", "yes", "on"):
        return "fastembed"
    if api_key:
        return "litellm"
    for env in _REMOTE_API_KEY_ENVS:
        if os.environ.get(env):
            return "litellm"
    return "fastembed"


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
        embedder: str | None = None,
    ) -> None:
        # sqlite-vec is required for both embedder kinds.
        try:
            import sqlite_vec  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "SqliteVecBackend requires the 'sqlite-vec' package. "
                "Install with: pip install 'nexus-ai-fs[sandbox]'"
            ) from exc

        # Pick the embedder. Explicit arg > env override > auto-detect.
        kind = (embedder or os.environ.get("NEXUS_EMBEDDER") or "auto").lower()
        if kind == "auto":
            kind = _detect_embedder_kind(api_key)
        if kind not in ("litellm", "fastembed"):
            raise ValueError(
                f"unknown embedder kind: {kind!r} (expected 'auto', 'litellm', 'fastembed')"
            )

        if kind == "litellm":
            try:
                import litellm  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "SqliteVecBackend(embedder='litellm') requires the 'litellm' package. "
                    "Install with: pip install 'nexus-ai-fs[sandbox]'"
                ) from exc
            self._embedding_model = embedding_model or os.environ.get(
                "NEXUS_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
            )
            self._embedding_dim = int(embedding_dim or DEFAULT_EMBEDDING_DIM)
        else:  # fastembed
            try:
                import fastembed  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "SqliteVecBackend(embedder='fastembed') requires the 'fastembed' package. "
                    "Install with: pip install 'nexus-ai-fs[sandbox]' (includes fastembed) "
                    "or set an embedding API key (e.g. OPENAI_API_KEY) to use litellm."
                ) from exc
            self._embedding_model = embedding_model or os.environ.get(
                "NEXUS_OFFLINE_EMBED_MODEL", DEFAULT_FASTEMBED_MODEL
            )
            self._embedding_dim = int(embedding_dim or DEFAULT_FASTEMBED_DIM)

        self._embedder_kind = kind
        self._fastembed_model: Any = None  # lazy ONNX session
        self._db_path = db_path
        self._api_key = api_key  # optional; litellm reads env by default
        self._conn: sqlite3.Connection | None = None
        # Locking strategy (Issue #3976, mirrors TxtaiBackend #3894):
        #   * Per-loop ``asyncio.Lock`` — Python 3.14 strictly enforces lock
        #     loop affinity; a single asyncio.Lock created in ``__init__``
        #     binds to whichever loop first acquires it and raises
        #     "bound to a different event loop" on every later acquire from
        #     another loop. The per-loop ``WeakKeyDictionary`` gives each
        #     loop its own lock so coroutine fairness still holds within
        #     a loop.
        #   * Process-wide ``threading.Lock`` — provides cross-loop
        #     mutual exclusion on the shared sqlite3 connection (which is
        #     not thread-safe even with ``check_same_thread=False``).
        #     Acquired *inside* the worker thread that ``asyncio.to_thread``
        #     dispatches so a cancelled caller cannot leave the lock held.
        self._startup_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._op_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._native_lock = threading.Lock()
        self._started = False

    def _get_loop_lock(
        self,
        cache: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock],
    ) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        # asyncio.Lock holds a strong ref to its bound loop after contention,
        # so the WeakKeyDictionary key alone cannot collect closed loops.
        # Drop entries whose loop is closed before we look up.
        for dead in [k for k in list(cache) if k.is_closed()]:
            cache.pop(dead, None)
        lock = cache.get(loop)
        if lock is None:
            lock = cache.setdefault(loop, asyncio.Lock())
        return lock

    async def _run_native(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` in a worker thread under the cross-loop native lock.

        The threading lock is taken *inside* the worker thread so a cancelled
        asyncio caller cannot release it while ``fn`` is still running.
        """

        def _inner() -> Any:
            with self._native_lock:
                return fn(*args, **kwargs)

        return await asyncio.to_thread(_inner)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def startup(self) -> None:
        """Open the SQLite connection, load sqlite-vec, ensure the table.

        Idempotent: a second call is a no-op.
        """
        async with self._get_loop_lock(self._startup_locks):
            if self._started:
                return

            def _dim_mismatch(existing_dim: int, *, race: bool) -> SqliteVecDimMismatchError:
                """Build a clear mismatch error. ``race=True`` means the
                table was created by a concurrent backend between our
                pre-check and our CREATE; the user message is otherwise
                identical."""
                race_note = (
                    " A concurrent SqliteVecBackend with a different "
                    "embedder created the table first; only one embedder "
                    "may share a DB."
                    if race
                    else ""
                )
                return SqliteVecDimMismatchError(
                    f"existing '{_VEC_TABLE}' table was created with "
                    f"embedding dim={existing_dim}, but this backend "
                    f"is configured for dim={self._embedding_dim} "
                    f"(model={self._embedding_model!r}, "
                    f"embedder={self._embedder_kind!r}).{race_note} "
                    f"sqlite-vec fixes the dim at table creation. "
                    f"Resolution: delete the '{_VEC_TABLE}' table "
                    f"(or the whole DB at {self._db_path}) to "
                    f"rebuild with the new embedder, or pin the "
                    f"original embedder via an embedding API key + "
                    f"NEXUS_EMBEDDING_MODEL / NEXUS_EMBEDDER=litellm."
                )

            def _read_existing_dim(conn: sqlite3.Connection) -> int | None:
                cur = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (_VEC_TABLE,),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                m = _DIM_REGEX.search(row[0])
                return int(m.group(1)) if m else None

            def _embedder_mismatch(
                stored_kind: str, stored_model: str, *, race: bool
            ) -> SqliteVecEmbedderMismatchError:
                race_note = (
                    " A concurrent SqliteVecBackend with a different "
                    "embedder identity registered first; only one embedder "
                    "may share a DB."
                    if race
                    else ""
                )
                return SqliteVecEmbedderMismatchError(
                    f"existing DB at {self._db_path!r} was populated by "
                    f"embedder={stored_kind!r} model={stored_model!r}, "
                    f"but this backend is configured for "
                    f"embedder={self._embedder_kind!r} "
                    f"model={self._embedding_model!r} "
                    f"(dim={self._embedding_dim}).{race_note} Two models "
                    f"with the same dim still live in different embedding "
                    f"spaces — mixing them silently corrupts KNN ranking. "
                    f"Resolution: delete the DB to rebuild with the new "
                    f"embedder, or pin the original embedder via "
                    f"NEXUS_EMBEDDER / NEXUS_EMBEDDING_MODEL / "
                    f"NEXUS_OFFLINE_EMBED_MODEL."
                )

            def _read_meta(conn: sqlite3.Connection) -> tuple[str, str] | None:
                """Return ``(embedder_kind, embedding_model)`` if the meta
                table exists AND has both rows; ``None`` on first start."""
                # Guard against the meta table not existing yet on a DB
                # populated by an older build that only had ``nexus_vec``.
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (_VEC_META_TABLE,),
                )
                if not cur.fetchone():
                    return None
                cur = conn.execute(
                    f"SELECT key, value FROM {_VEC_META_TABLE} "
                    f"WHERE key IN ('embedder_kind', 'embedding_model')"
                )
                kv = {row[0]: row[1] for row in cur.fetchall()}
                k = kv.get("embedder_kind")
                m = kv.get("embedding_model")
                return (k, m) if k is not None and m is not None else None

            def _open() -> sqlite3.Connection:
                # check_same_thread=False because we hop through to_thread
                # and the executor's worker may differ between calls.
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                # Codex review R2 (high): wait through another backend's
                # CREATE under concurrent first-start instead of failing
                # fast with SQLITE_BUSY.
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.enable_load_extension(True)
                import sqlite_vec

                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                # Codex review R1 (high): pre-check — when the table
                # already exists, validate its embedding dim against the
                # backend's configured dim BEFORE CREATE IF NOT EXISTS
                # (which would silently no-op). Catches the common case
                # quickly with a clear error.
                existing = _read_existing_dim(conn)
                if existing is not None and existing != self._embedding_dim:
                    raise _dim_mismatch(existing, race=False)
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
                # Codex review R2 (high): post-check — re-read the
                # schema after CREATE to catch the race where a
                # concurrent backend with a different dim created the
                # table during our pre-check window. Without this, the
                # losing backend would mark itself started with a dim
                # that does not match the actual table and silently fail
                # every later upsert/search.
                created = _read_existing_dim(conn)
                if created is not None and created != self._embedding_dim:
                    raise _dim_mismatch(created, race=True)
                # Codex review R3 (medium) + R4 (high): persist + validate
                # the embedder identity (kind + model). The dim check above
                # cannot tell two same-dim models apart — silently mixing
                # them in the same vec0 table corrupts KNN ranking.
                #
                # Three states must be distinguished BEFORE we INSERT:
                #   (a) brand-new DB (vec table empty AND meta absent or
                #       empty): safe to register OUR identity as the
                #       table owner.
                #   (b) populated DB with valid meta: validate ours
                #       matches; raise mismatch otherwise.
                #   (c) populated DB with NO meta — pre-R3 upgrade path:
                #       FAIL CLOSED. The existing rows could have been
                #       written by any embedder, and blindly tagging
                #       them with the current backend's identity would
                #       silently bless an incompatible embedder if the
                #       original differed. Force the operator to
                #       rebuild rather than risk silent corruption.
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_VEC_META_TABLE} ("
                    f"key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                # Read meta + row count BEFORE any INSERT so we can tell
                # state (c) apart from state (a).
                pre_meta = _read_meta(conn)
                vec_has_rows = (
                    conn.execute(f"SELECT 1 FROM {_VEC_TABLE} LIMIT 1").fetchone() is not None
                )
                if pre_meta is None and vec_has_rows:
                    # State (c): pre-R3 DB upgrade. Refuse to bless.
                    raise SqliteVecEmbedderMismatchError(
                        f"existing DB at {self._db_path!r} has rows in "
                        f"'{_VEC_TABLE}' but no embedder identity recorded "
                        f"in '{_VEC_META_TABLE}' (pre-R3 build). The "
                        f"existing rows may have been written by any "
                        f"embedder; tagging them with the current backend "
                        f"({self._embedder_kind!r}, "
                        f"{self._embedding_model!r}) would risk silent "
                        f"KNN ranking corruption if the original differed. "
                        f"Resolution: delete the DB at {self._db_path} "
                        f"(or just drop '{_VEC_TABLE}' and "
                        f"'{_VEC_META_TABLE}') to rebuild from scratch."
                    )
                # States (a) + (b): safe to INSERT OR IGNORE. The IGNORE
                # makes the first writer win under concurrent first-
                # start; the post-write SELECT then reveals which
                # identity actually got persisted.
                conn.executemany(
                    f"INSERT OR IGNORE INTO {_VEC_META_TABLE}(key, value) VALUES (?, ?)",
                    [
                        ("embedder_kind", self._embedder_kind),
                        ("embedding_model", self._embedding_model),
                    ],
                )
                conn.commit()
                meta = _read_meta(conn)
                if meta is not None:
                    stored_kind, stored_model = meta
                    if stored_kind != self._embedder_kind or stored_model != self._embedding_model:
                        # Whether this is a steady-state mismatch (DB
                        # populated by a different embedder days ago) or
                        # a true race (concurrent first-start, the other
                        # backend's INSERT OR IGNORE landed first) is
                        # not user-distinguishable and the resolution is
                        # the same in both cases. Flag race=True if at
                        # least one field still matches ours — that's
                        # the only scenario where a concurrent open is
                        # a plausible explanation.
                        race_now = (
                            stored_kind == self._embedder_kind
                            or stored_model == self._embedding_model
                        )
                        raise _embedder_mismatch(stored_kind, stored_model, race=race_now)
                return conn

            self._conn = await self._run_native(_open)
            self._started = True
            logger.info(
                "[SqliteVecBackend] started (db=%s embedder=%s model=%s dim=%d)",
                self._db_path,
                self._embedder_kind,
                self._embedding_model,
                self._embedding_dim,
            )

    async def shutdown(self) -> None:
        """Close the SQLite connection."""
        async with self._get_loop_lock(self._startup_locks):
            if self._conn is not None:
                conn = self._conn
                self._conn = None
                self._started = False
                await self._run_native(conn.close)

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
        """Embed a single string. Dispatches by ``self._embedder_kind``."""
        if self._embedder_kind == "fastembed":
            vecs = await self._embed_many([text])
            return vecs[0] if vecs else []
        import litellm

        kwargs: dict[str, Any] = {"model": self._embedding_model, "input": [text]}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        resp = await litellm.aembedding(**kwargs)
        # resp.data is a list of {"embedding": [...], "index": ...} dicts.
        vec = resp.data[0]["embedding"]
        return list(vec)

    async def _embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings. Dispatches by ``self._embedder_kind``."""
        if not texts:
            return []
        if self._embedder_kind == "fastembed":
            return await self._fastembed_many(texts)
        import litellm

        kwargs: dict[str, Any] = {"model": self._embedding_model, "input": texts}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        resp = await litellm.aembedding(**kwargs)
        return [list(item["embedding"]) for item in resp.data]

    async def _fastembed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed via fastembed (sync ONNX). Lazy-loads model on first call."""
        if self._fastembed_model is None:
            await self._init_fastembed_model()
        model = self._fastembed_model

        def _run() -> list[list[float]]:
            # ``model.embed`` returns an iterator of numpy.ndarray[float32].
            return [list(map(float, vec)) for vec in model.embed(list(texts))]

        return await asyncio.to_thread(_run)

    async def _init_fastembed_model(self) -> None:
        """Lazy-init the fastembed TextEmbedding model in a worker thread.

        ONNX session construction does network I/O (model download on
        first run) and CPU work, so we hop off the loop. Idempotent.
        """
        if self._fastembed_model is not None:
            return

        model_name = self._embedding_model

        def _open() -> Any:
            from fastembed import TextEmbedding

            return TextEmbedding(model_name=model_name)

        model = await asyncio.to_thread(_open)
        # Double-init is harmless (we'd just keep the latter model);
        # don't bother with a lock to keep the cold path simple.
        self._fastembed_model = model
        logger.info(
            "[SqliteVecBackend] fastembed model loaded (model=%s dim=%d)",
            self._embedding_model,
            self._embedding_dim,
        )

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

        async with self._get_loop_lock(self._op_locks):
            written: int = await self._run_native(_write)
            return written

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

        async with self._get_loop_lock(self._op_locks):
            swapped: int = await self._run_native(_swap)
            return swapped

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

        async with self._get_loop_lock(self._op_locks):
            deleted: int = await self._run_native(_delete)
            return deleted

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

        async with self._get_loop_lock(self._op_locks):
            rows = await self._run_native(_query)
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

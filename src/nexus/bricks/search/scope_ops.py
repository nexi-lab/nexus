"""Per-directory semantic index scope operations (Issue #3698).

Free-function implementations of the daemon's index-scope CRUD. Lives
outside ``daemon.py`` to keep that file under the source-file size limit
and because these operations are orthogonal to the daemon's hot-path
query/refresh loops — moving them out makes the daemon's surface smaller
and the scope logic easier to find.

Each function takes a ``SearchDaemon`` as the first argument and mutates
its in-memory state under ``_refresh_lock`` while writing through to the
database. The daemon exposes thin wrapper methods that delegate here.

See the 8 Issue #6 policies documented per function and in
``tests/unit/bricks/search/test_daemon_scope_crud.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text as sa_text

from nexus.bricks.search.index_scope import (
    INDEX_MODE_ALL,
    INDEX_MODE_SCOPED,
    DirectoryAlreadyRegisteredError,
    DirectoryNotRegisteredError,
    InvalidDirectoryPathError,
    ZoneNotFoundError,
    canonical_directory_path,
)

if TYPE_CHECKING:
    from nexus.bricks.search.daemon import SearchDaemon

logger = logging.getLogger(__name__)

__all__ = [
    "add_indexed_directory",
    "list_indexed_directories",
    "purge_unscoped_embeddings",
    "remove_indexed_directory",
    "set_zone_indexing_mode",
    "validate_directory_path",
    "zone_exists",
]


def validate_directory_path(directory_path: str) -> str:
    """Canonicalize a directory path and reject malformed input.

    Enforces the Issue #6 policies:
    - Path must be absolute (starts with '/')
    - Path must not contain ``..`` or ``.`` segments (escape attempts)
    - Path must not include the ``/zone/{id}/`` wrapper — callers pass
      the virtual path only
    - Trailing slashes are stripped (except for the root '/')

    Returns the canonical form. Raises ``InvalidDirectoryPathError``
    on any violation.
    """
    if not directory_path:
        raise InvalidDirectoryPathError("directory_path must not be empty")
    if not directory_path.startswith("/"):
        raise InvalidDirectoryPathError(f"directory_path must be absolute, got {directory_path!r}")

    # Reject ``..`` and ``.`` segments outright — easier to reason about
    # than normpath and prevents subtle escape attempts.
    segments = [seg for seg in directory_path.split("/") if seg]
    if ".." in segments:
        raise InvalidDirectoryPathError(
            f"directory_path must not contain '..', got {directory_path!r}"
        )
    if "." in segments:
        raise InvalidDirectoryPathError(
            f"directory_path must not contain '.', got {directory_path!r}"
        )

    try:
        return canonical_directory_path(directory_path)
    except ValueError as exc:
        raise InvalidDirectoryPathError(str(exc)) from exc


async def zone_exists(daemon: SearchDaemon, zone_id: str) -> bool:
    """Return True iff the zone has an active row in ``zones``."""
    session_factory = daemon._async_session
    if session_factory is None:
        # Test scaffolding without a DB — assume zones exist so the
        # daemon doesn't block in unit tests.
        return True
    async with session_factory() as session:
        row = (
            await session.execute(
                sa_text("SELECT 1 FROM zones WHERE zone_id = :zid AND deleted_at IS NULL"),
                {"zid": zone_id},
            )
        ).first()
    return row is not None


async def add_indexed_directory(daemon: SearchDaemon, zone_id: str, directory_path: str) -> str:
    """Register ``directory_path`` for scoped indexing under ``zone_id``.

    Policies (Issue #6): #1 non-existent dirs allowed, #4 missing zone
    → ``ZoneNotFoundError``, #5 path escape → ``InvalidDirectoryPathError``.
    Mutates in-memory + DB state under ``_refresh_lock``.
    """
    canonical = validate_directory_path(directory_path)

    if not await zone_exists(daemon, zone_id):
        raise ZoneNotFoundError(f"zone {zone_id!r} does not exist")

    async with daemon._refresh_lock:
        existing = daemon._indexed_directories.get(zone_id, set())
        if canonical in existing:
            raise DirectoryAlreadyRegisteredError(
                f"directory {canonical!r} is already registered for zone {zone_id!r}"
            )

        if daemon._async_session is not None:
            async with daemon._async_session() as session:
                # Explicit ``created_at`` — the Alembic migration sets
                # ``server_default=sa.func.now()`` but SQLAlchemy raw
                # ``text()`` INSERTs bypass the DB-level default, so we
                # supply the timestamp from Python here.
                await session.execute(
                    sa_text(
                        "INSERT INTO indexed_directories "
                        "(zone_id, directory_path, created_at) "
                        "VALUES (:zid, :dp, :ts)"
                    ),
                    {
                        "zid": zone_id,
                        "dp": canonical,
                        "ts": datetime.now(UTC),
                    },
                )
                await session.commit()

        daemon._indexed_directories.setdefault(zone_id, set()).add(canonical)

    logger.info("Registered indexed directory %s under zone %s", canonical, zone_id)
    return canonical


async def remove_indexed_directory(daemon: SearchDaemon, zone_id: str, directory_path: str) -> str:
    """Unregister ``directory_path`` from scoped indexing.

    Does NOT purge existing embeddings — use ``purge_unscoped_embeddings``
    for that. Policies (Issue #6): #6 unregistering an absent entry
    raises ``DirectoryNotRegisteredError``.
    """
    canonical = validate_directory_path(directory_path)

    async with daemon._refresh_lock:
        existing = daemon._indexed_directories.get(zone_id, set())
        if canonical not in existing:
            raise DirectoryNotRegisteredError(
                f"directory {canonical!r} is not registered for zone {zone_id!r}"
            )

        if daemon._async_session is not None:
            async with daemon._async_session() as session:
                await session.execute(
                    sa_text(
                        "DELETE FROM indexed_directories "
                        "WHERE zone_id = :zid AND directory_path = :dp"
                    ),
                    {"zid": zone_id, "dp": canonical},
                )
                await session.commit()

        existing.discard(canonical)
        if not existing:
            # Drop empty sets so _current_index_scope doesn't carry
            # stale keys.
            daemon._indexed_directories.pop(zone_id, None)

    logger.info("Unregistered indexed directory %s under zone %s", canonical, zone_id)
    return canonical


def list_indexed_directories(daemon: SearchDaemon, zone_id: str) -> list[str]:
    """Return registered directories for ``zone_id`` in sorted order."""
    return sorted(daemon._indexed_directories.get(zone_id, set()))


async def set_zone_indexing_mode(daemon: SearchDaemon, zone_id: str, mode: str) -> None:
    """Flip a zone between ``'all'`` and ``'scoped'`` indexing modes."""
    if mode not in (INDEX_MODE_ALL, INDEX_MODE_SCOPED):
        raise InvalidDirectoryPathError(f"mode must be 'all' or 'scoped', got {mode!r}")

    if not await zone_exists(daemon, zone_id):
        raise ZoneNotFoundError(f"zone {zone_id!r} does not exist")

    async with daemon._refresh_lock:
        if daemon._async_session is not None:
            async with daemon._async_session() as session:
                await session.execute(
                    sa_text("UPDATE zones SET indexing_mode = :mode WHERE zone_id = :zid"),
                    {"mode": mode, "zid": zone_id},
                )
                await session.commit()
        daemon._zone_indexing_modes[zone_id] = mode

    logger.info("Set indexing_mode=%s for zone %s", mode, zone_id)


async def purge_unscoped_embeddings(
    daemon: SearchDaemon, zone_id: str | None = None
) -> dict[str, int]:
    """Delete derived embedding artifacts for files now outside any scope.

    Destructive admin operation. Only zones in ``'scoped'`` mode are
    affected — zones in ``'all'`` mode keep all their embeddings.

    **What gets deleted:** only the **derived** txtai artifacts
    (``sections``, ``vectors``, and the in-memory txtai index) for rows
    whose ``id`` corresponds to a file that no longer falls under any
    registered ``indexed_directories`` row.

    **What does NOT get deleted:** the canonical ``document_chunks``
    table. Those rows are the source-of-truth for semantic search and
    are what ``_bootstrap_txtai_backend`` replays on every daemon
    restart. Deleting them would turn a scope mode flip into permanent,
    unrecoverable search-data loss: switching a zone back from
    ``'scoped'`` to ``'all'`` could not re-populate those files without
    a full re-embed (which may be impossible if the source file
    contents have since changed).

    The txtai artifacts, by contrast, are cheap to rebuild from
    ``document_chunks`` via the next bootstrap — so purging them only
    reclaims memory and search-index space, not data.

    Returns ``{"txtai_docs": M}`` where M is the number of stale
    txtai rows removed. ``document_chunks`` is reported as 0 for
    backward compat with the response shape but is never written to.
    """
    counts = {"document_chunks": 0, "txtai_docs": 0}

    if daemon._async_session is None:
        logger.debug("No session available; skipping purge")
        return counts

    target_zones = [
        zid
        for zid, mode in daemon._zone_indexing_modes.items()
        if mode == "scoped" and (zone_id is None or zid == zone_id)
    ]
    if not target_zones:
        return counts

    async with daemon._refresh_lock:
        for target_zone in target_zones:
            # We purge ONLY the txtai content store (``sections``) plus
            # the in-memory txtai index. The ``document_chunks`` table
            # is the canonical rebuild source and MUST NOT be touched
            # here — see the function docstring for rationale.
            async with daemon._async_session() as session:
                prefix_like = f"{target_zone}:%" if target_zone != "root" else None
                candidate_sql = """
                    SELECT id FROM sections
                    WHERE (
                        id LIKE '/%'
                        OR id LIKE :zone_prefix
                    )
                """
                txtai_rows = (
                    await session.execute(
                        sa_text(candidate_sql),
                        {"zone_prefix": prefix_like or "__never_matches__"},
                    )
                ).fetchall()

                # Strip optional zone: prefix and evaluate scope in Python
                # (simpler than rewriting the full path-match logic in SQL
                # for the txtai id format).
                from nexus.bricks.search.index_scope import is_path_indexed

                scope = daemon._current_index_scope()
                txtai_ids_to_delete: list[str] = []
                for (sec_id,) in txtai_rows:
                    if not isinstance(sec_id, str):
                        continue
                    # Extract the virtual_path portion.
                    if ":" in sec_id and not sec_id.startswith("/"):
                        zid_prefix, _, vpath = sec_id.partition(":")
                        if zid_prefix != target_zone:
                            continue
                        candidate_vpath = vpath
                    elif sec_id.startswith("/"):
                        if target_zone != "root":
                            # Unprefixed ids belong to the root zone.
                            continue
                        candidate_vpath = sec_id
                    else:
                        continue
                    if scope is None:
                        continue
                    try:
                        if not is_path_indexed(scope, target_zone, candidate_vpath):
                            txtai_ids_to_delete.append(sec_id)
                    except ValueError:
                        continue

            # Prune the txtai backend via its delete API so the in-memory
            # index and pgvector rows both drop the entries.
            if daemon._backend is not None and txtai_ids_to_delete:
                try:
                    deleted = int(
                        await daemon._backend.delete(txtai_ids_to_delete, zone_id=target_zone)
                    )
                    counts["txtai_docs"] += deleted
                except Exception:
                    logger.warning(
                        "Failed to prune txtai backend during purge for zone %s",
                        target_zone,
                        exc_info=True,
                    )

    logger.info(
        "Purged unscoped txtai artifacts: %d docs across %d zones "
        "(document_chunks preserved for rebuild)",
        counts["txtai_docs"],
        len(target_zones),
    )
    return counts

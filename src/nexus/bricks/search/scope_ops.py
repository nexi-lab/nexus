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
from typing import TYPE_CHECKING, Any

from sqlalchemy import bindparam
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
                await session.execute(
                    sa_text(
                        "INSERT INTO indexed_directories "
                        "(zone_id, directory_path) VALUES (:zid, :dp)"
                    ),
                    {"zid": zone_id, "dp": canonical},
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
    """Delete stored embeddings for files that are now outside any scope.

    Destructive admin operation. Only zones in ``'scoped'`` mode are
    affected — zones in ``'all'`` mode keep all their embeddings.
    Returns ``{"document_chunks": N, "txtai_docs": M}``.
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
            async with daemon._async_session() as session:
                rows = (
                    await session.execute(
                        sa_text(
                            """
                            SELECT fp.path_id, fp.virtual_path
                            FROM file_paths fp
                            WHERE fp.zone_id = :zid
                              AND fp.deleted_at IS NULL
                              AND EXISTS (
                                SELECT 1 FROM document_chunks c
                                WHERE c.path_id = fp.path_id
                              )
                              AND NOT EXISTS (
                                SELECT 1 FROM indexed_directories d
                                WHERE d.zone_id = fp.zone_id
                                  AND (
                                    d.directory_path = '/'
                                    OR fp.virtual_path = d.directory_path
                                    OR fp.virtual_path LIKE d.directory_path || '/%'
                                  )
                              )
                            """
                        ),
                        {"zid": target_zone},
                    )
                ).fetchall()

                if not rows:
                    continue

                path_ids = [row[0] for row in rows]
                virtual_paths = [row[1] for row in rows]

                delete_stmt = sa_text(
                    "DELETE FROM document_chunks WHERE path_id IN :pids"
                ).bindparams(bindparam("pids", expanding=True))
                result = await session.execute(delete_stmt, {"pids": path_ids})
                await session.commit()
                counts["document_chunks"] += int(result.rowcount or 0)

            # Prune the txtai backend too so searches don't return stale hits.
            if daemon._backend is not None:
                try:
                    ids: list[Any] = [
                        f"{target_zone}:{vp}" if target_zone != "root" else vp
                        for vp in virtual_paths
                    ]
                    deleted = int(await daemon._backend.delete(ids, zone_id=target_zone))
                    counts["txtai_docs"] += deleted
                except Exception:
                    logger.warning(
                        "Failed to prune txtai backend during purge for zone %s",
                        target_zone,
                        exc_info=True,
                    )

    logger.info(
        "Purged unscoped embeddings: %d chunks, %d txtai docs across %d zones",
        counts["document_chunks"],
        counts["txtai_docs"],
        len(target_zones),
    )
    return counts

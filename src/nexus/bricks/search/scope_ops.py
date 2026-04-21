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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text as sa_text

from nexus.bricks.search.index_scope import (
    INDEX_MODE_ALL,
    INDEX_MODE_SCOPED,
    DirectoryAlreadyRegisteredError,
    DirectoryNotRegisteredError,
    IndexScopeError,
    InvalidDirectoryPathError,
    ZoneNotFoundError,
    canonical_directory_path,
)
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.bricks.search.daemon import SearchDaemon

logger = logging.getLogger(__name__)

__all__ = [
    "BackfillFailedError",
    "BackfillResult",
    "add_indexed_directory",
    "backfill_zone_from_chunks",
    "list_indexed_directories",
    "purge_unscoped_embeddings",
    "remove_indexed_directory",
    "rerun_backfill_for_directory",
    "set_zone_indexing_mode",
    "validate_directory_path",
    "zone_exists",
]


@dataclass(frozen=True)
class BackfillResult:
    """Outcome of a ``backfill_zone_from_chunks`` call.

    The previous int return type couldn't distinguish "no rows to
    backfill" from "concurrent scope mutation made us bail" — both
    looked like 0 to the caller, hiding a real partial-failure mode.
    The dataclass exposes the status explicitly so routers can
    surface degraded states properly.

    Statuses:
      - ``ok``      — backfill completed (``files`` may be 0 if the
                       zone genuinely had no in-scope chunks).
      - ``skewed``  — generation guard fired; a concurrent mutation
                       superseded this backfill. Caller should treat
                       as degraded and prompt operator retry.
      - ``no_op``   — daemon has no DB session or backend; nothing
                       to do, not an error.
    """

    status: str  # 'ok' | 'skewed' | 'no_op'
    files: int


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


async def add_indexed_directory(
    daemon: SearchDaemon, zone_id: str, directory_path: str
) -> tuple[str, BackfillResult]:
    """Register ``directory_path`` for scoped indexing under ``zone_id``.

    Policies (Issue #6): #1 non-existent dirs allowed, #4 missing zone
    → ``ZoneNotFoundError``, #5 path escape → ``InvalidDirectoryPathError``.
    Mutates in-memory + DB state under ``_refresh_lock``.

    After successful registration, runs a backfill from
    ``document_chunks`` so files that already exist under the new
    directory become semantically searchable immediately rather than
    waiting for the next write or restart.

    Returns ``(canonical_path, BackfillResult)``. The router uses the
    ``BackfillResult.status`` to distinguish ``ok`` (clean success)
    from ``skewed`` (concurrent mutation superseded the backfill —
    operator should retry via :func:`rerun_backfill_for_directory`).
    Hard backfill failure raises ``BackfillFailedError`` which
    propagates to the router as a degraded response.
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
        daemon._scope_generation += 1
        backfill_generation = daemon._scope_generation

    logger.info("Registered indexed directory %s under zone %s", canonical, zone_id)

    # Backfill OUTSIDE the refresh lock so the txtai upsert (which can
    # take seconds) doesn't block other refreshes. Pass the captured
    # generation so the backfill bails if scope changes mid-flight,
    # AND pass the canonical directory_path so the SQL is prefix-
    # scoped instead of scanning the whole zone.
    result = await backfill_zone_from_chunks(
        daemon,
        zone_id,
        expected_generation=backfill_generation,
        directory_path=canonical,
    )

    return canonical, result


async def remove_indexed_directory(daemon: SearchDaemon, zone_id: str, directory_path: str) -> str:
    """Unregister ``directory_path`` from scoped indexing.

    Does NOT purge existing embeddings — the caller (typically the
    HTTP endpoint) follows up with ``purge_unscoped_embeddings`` so
    the auto-purge happens at the API boundary. Policies (Issue #6):
    #6 unregistering an absent entry raises ``DirectoryNotRegisteredError``.
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
        # Bump generation so any in-flight backfill captures it and bails.
        daemon._scope_generation += 1

    logger.info("Unregistered indexed directory %s under zone %s", canonical, zone_id)
    return canonical


def list_indexed_directories(daemon: SearchDaemon, zone_id: str) -> list[str]:
    """Return registered directories for ``zone_id`` in sorted order."""
    return sorted(daemon._indexed_directories.get(zone_id, set()))


class BackfillFailedError(IndexScopeError):
    """Raised when a scope-expansion backfill fails to durably write txtai.

    Distinct from "nothing to backfill" (returns 0) so callers can
    surface a partial-success / degraded state to operators instead of
    silently swallowing the failure.
    """

    def __init__(self, message: str, *, files_attempted: int) -> None:
        super().__init__(message)
        self.files_attempted = files_attempted


async def backfill_zone_from_chunks(
    daemon: SearchDaemon,
    zone_id: str,
    expected_generation: int | None = None,
    directory_path: str | None = None,
) -> BackfillResult:
    """Re-upsert in-scope ``document_chunks`` for ``zone_id`` into txtai.

    Called when scope expands (a directory is registered, or a zone
    flips from ``'scoped'`` back to ``'all'``). Without backfill, files
    that were previously skipped by the embedding pipeline would stay
    invisible to semantic search until they were re-written or the
    daemon was restarted (because bootstrap is the only other path
    that replays ``document_chunks`` into txtai).

    **Prefix scoping**: when ``directory_path`` is provided, the SQL
    query is restricted to files under that prefix only — using the
    same wildcard-escaping rules as the bootstrap path. This prevents
    a single ``add_indexed_directory`` call from re-embedding the
    entire zone (which would be a latency bomb on large workspaces
    AND blow up embedding API budget). Without ``directory_path``,
    falls back to a full-zone scan, which is the right behavior for
    the ``scoped → all`` mode flip path.

    **Skip when zone is in 'all' mode**: zones in ``'all'`` mode
    embed everything by default — there is nothing to backfill on
    register because the directory was already covered. Returns
    ``BackfillResult(status='ok', files=0)`` immediately.

    **Generation guard**: ``expected_generation`` should be captured by
    the caller while holding ``_refresh_lock`` immediately after the
    scope mutation that triggered this backfill. The backfill checks
    ``daemon._scope_generation`` again immediately before issuing the
    txtai upsert; if the generation has advanced (a concurrent
    unregister/mode-flip happened), the backfill bails with
    ``BackfillResult(status='skewed')`` so the caller can surface the
    superseded outcome instead of pretending it succeeded.

    Returns a ``BackfillResult``:
      - ``ok`` — backfill ran to completion (``files`` may be 0 if
        the zone genuinely had no in-scope chunks).
      - ``skewed`` — concurrent scope mutation; we did not upsert.
      - ``no_op`` — daemon has no DB or backend wired (test mode).

    Hard failures (SELECT or upsert raise) propagate as
    ``BackfillFailedError``.
    """
    if daemon._async_session is None or daemon._backend is None:
        return BackfillResult(status="no_op", files=0)

    from nexus.bricks.search.index_scope import (
        INDEX_MODE_ALL,
        canonical_directory_path,
        is_path_indexed,
    )

    scope = daemon._current_index_scope()
    if scope is None:
        return BackfillResult(status="no_op", files=0)

    # Skip backfill entirely when the call originates from a
    # directory-register (``directory_path`` set) AND the zone is in
    # 'all' mode — every file is already in scope, so registering a
    # directory adds no new files to embed. The mode-flip path
    # (``directory_path is None``) intentionally runs even in 'all'
    # mode because that's when previously-skipped files need to be
    # replayed into txtai.
    current_mode = daemon._zone_indexing_modes.get(zone_id, INDEX_MODE_ALL)
    if directory_path is not None and current_mode == INDEX_MODE_ALL:
        logger.debug(
            "backfill_zone_from_chunks: zone %s is in 'all' mode; "
            "skipping prefix backfill (everything already in scope)",
            zone_id,
        )
        return BackfillResult(status="ok", files=0)

    # Build the SELECT. When ``directory_path`` is set we push the
    # prefix filter into SQL so we don't materialize the whole zone
    # in Python. The escape pattern matches the bootstrap path so
    # ``_`` and ``%`` in directory names cannot be interpreted as
    # SQL wildcards.
    canonical_dir: str | None = None
    if directory_path is not None:
        try:
            canonical_dir = canonical_directory_path(directory_path)
        except ValueError as exc:
            raise BackfillFailedError(
                f"invalid directory_path for backfill: {exc}",
                files_attempted=0,
            ) from exc

    try:
        async with daemon._async_session() as session:
            if canonical_dir is None:
                rows = (
                    await session.execute(
                        sa_text(
                            """
                            SELECT
                                fp.virtual_path,
                                c.chunk_index,
                                c.chunk_text
                            FROM document_chunks c
                            JOIN file_paths fp ON c.path_id = fp.path_id
                            WHERE fp.zone_id = :zid
                              AND fp.deleted_at IS NULL
                            ORDER BY fp.virtual_path, c.chunk_index
                            """
                        ),
                        {"zid": zone_id},
                    )
                ).fetchall()
            else:
                # Prefix-scoped path. Match either the directory itself
                # (rare — directories don't usually have chunks) OR a
                # descendant via LIKE escape, identical to the bootstrap
                # rule that prevents '/src' from matching '/srcX/foo'.
                rows = (
                    await session.execute(
                        sa_text(
                            r"""
                            SELECT
                                fp.virtual_path,
                                c.chunk_index,
                                c.chunk_text
                            FROM document_chunks c
                            JOIN file_paths fp ON c.path_id = fp.path_id
                            WHERE fp.zone_id = :zid
                              AND fp.deleted_at IS NULL
                              AND (
                                fp.virtual_path = :dp
                                OR fp.virtual_path LIKE
                                    REPLACE(
                                      REPLACE(
                                        REPLACE(:dp, '\', '\\'),
                                        '%', '\%'
                                      ),
                                      '_', '\_'
                                    ) || '/%' ESCAPE '\'
                              )
                            ORDER BY fp.virtual_path, c.chunk_index
                            """
                        ),
                        {"zid": zone_id, "dp": canonical_dir},
                    )
                ).fetchall()
    except Exception as exc:
        logger.warning(
            "backfill_zone_from_chunks: SELECT failed for zone %s",
            zone_id,
            exc_info=True,
        )
        raise BackfillFailedError(
            f"failed to read document_chunks for zone {zone_id!r}: {exc}",
            files_attempted=0,
        ) from exc

    if not rows:
        return BackfillResult(status="ok", files=0)

    # Group chunks by virtual_path and filter through scope. Use the
    # CURRENT scope (not the captured snapshot) so per-file filtering
    # always reflects the latest in-memory state.
    current_scope = daemon._current_index_scope()
    if current_scope is None:
        return BackfillResult(status="no_op", files=0)
    grouped: dict[str, list[str]] = {}
    for row in rows:
        vpath = row[0]
        text = row[2] or ""
        if not vpath:
            continue
        try:
            if not is_path_indexed(current_scope, zone_id, vpath):
                continue
        except ValueError:
            continue
        grouped.setdefault(vpath, []).append(text)

    if not grouped:
        return BackfillResult(status="ok", files=0)

    docs: list[dict[str, Any]] = []
    for vpath, parts in grouped.items():
        content = "\n".join(p for p in parts if p)
        if not content.strip():
            continue
        doc_id = f"{zone_id}:{vpath}" if zone_id != ROOT_ZONE_ID else vpath
        docs.append(
            {
                "id": doc_id,
                "text": content,
                "path": vpath,
                "zone_id": zone_id,
            }
        )

    if not docs:
        return BackfillResult(status="ok", files=0)

    # Generation check IMMEDIATELY before the upsert. If a concurrent
    # unregister/mode-flip bumped the generation while we were
    # SELECTing/grouping, bail without upserting — those documents
    # have just become out-of-scope and we must not resurrect them.
    if expected_generation is not None and daemon._scope_generation != expected_generation:
        logger.info(
            "backfill_zone_from_chunks: scope generation advanced "
            "(%d → %d) for zone %s while backfilling; bailing without "
            "upsert to avoid resurrecting newly out-of-scope files",
            expected_generation,
            daemon._scope_generation,
            zone_id,
        )
        return BackfillResult(status="skewed", files=0)

    try:
        await daemon._backend.upsert(docs, zone_id=zone_id)
    except Exception as exc:
        logger.warning(
            "backfill_zone_from_chunks: txtai upsert failed for zone %s",
            zone_id,
            exc_info=True,
        )
        raise BackfillFailedError(
            f"failed to upsert {len(docs)} files into txtai for zone {zone_id!r}: {exc}",
            files_attempted=len(docs),
        ) from exc

    logger.info(
        "backfilled %d in-scope files into txtai for zone %s",
        len(docs),
        zone_id,
    )
    return BackfillResult(status="ok", files=len(docs))


async def rerun_backfill_for_directory(
    daemon: SearchDaemon,
    zone_id: str,
    directory_path: str,
) -> BackfillResult:
    """Re-trigger backfill for an already-registered directory.

    Used by the recovery path when ``add_indexed_directory`` was
    called against a directory that is already registered. Without
    this, the duplicate-registration check would return 409 and
    the operator would have no way to retry a failed backfill
    short of unregister + re-register (which is destructive).

    Captures a fresh generation token under the refresh lock and
    runs the backfill. Returns the same ``BackfillResult`` shape so
    the caller can distinguish ``ok`` from ``skewed`` from failure.
    """
    canonical = validate_directory_path(directory_path)

    async with daemon._refresh_lock:
        # Verify the directory IS registered. We surface a clear
        # message if not — the caller should use add_indexed_directory
        # in that case (which itself triggers backfill).
        existing = daemon._indexed_directories.get(zone_id, set())
        if canonical not in existing:
            raise DirectoryNotRegisteredError(
                f"directory {canonical!r} is not registered for zone {zone_id!r}; "
                f"use add_indexed_directory instead"
            )
        backfill_generation = daemon._scope_generation

    # Pass the canonical directory_path so the backfill is prefix-
    # scoped instead of scanning the entire zone — important for
    # recovery on large workspaces where the broad scan would be a
    # latency / embedding-cost bomb.
    return await backfill_zone_from_chunks(
        daemon,
        zone_id,
        expected_generation=backfill_generation,
        directory_path=canonical,
    )


async def set_zone_indexing_mode(
    daemon: SearchDaemon, zone_id: str, mode: str
) -> BackfillResult | None:
    """Flip a zone between ``'all'`` and ``'scoped'`` indexing modes.

    When flipping from ``'scoped'`` to ``'all'``, kicks off a backfill
    from ``document_chunks`` so previously skipped files become
    semantically searchable immediately. The router calls
    ``purge_unscoped_embeddings`` after the reverse flip
    (``'all'`` → ``'scoped'``) to keep both directions symmetric.

    Returns the ``BackfillResult`` from the widening backfill, or
    ``None`` when no backfill ran (mode unchanged or shrinking flip).
    Hard backfill failure raises ``BackfillFailedError``.
    """
    if mode not in (INDEX_MODE_ALL, INDEX_MODE_SCOPED):
        raise InvalidDirectoryPathError(f"mode must be 'all' or 'scoped', got {mode!r}")

    if not await zone_exists(daemon, zone_id):
        raise ZoneNotFoundError(f"zone {zone_id!r} does not exist")

    previous_mode: str | None = None
    backfill_generation: int | None = None
    async with daemon._refresh_lock:
        previous_mode = daemon._zone_indexing_modes.get(zone_id)
        if daemon._async_session is not None:
            async with daemon._async_session() as session:
                await session.execute(
                    sa_text("UPDATE zones SET indexing_mode = :mode WHERE zone_id = :zid"),
                    {"mode": mode, "zid": zone_id},
                )
                await session.commit()
        daemon._zone_indexing_modes[zone_id] = mode
        daemon._scope_generation += 1
        backfill_generation = daemon._scope_generation

    logger.info("Set indexing_mode=%s for zone %s", mode, zone_id)

    # Backfill on widening (scoped → all) so previously excluded files
    # become searchable without waiting for the next write/restart.
    # Done outside the refresh lock so the txtai upsert doesn't block
    # other refreshes. Pass the captured generation so the backfill
    # bails if scope changes mid-flight.
    if previous_mode == INDEX_MODE_SCOPED and mode == INDEX_MODE_ALL:
        return await backfill_zone_from_chunks(
            daemon, zone_id, expected_generation=backfill_generation
        )
    return None


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
            #
            # **Fail-closed**: do NOT swallow backend.delete failures.
            # ``TxtaiBackend.delete`` raises if persistence to pgvector
            # fails; we re-raise from here so the HTTP endpoint surfaces
            # a 5xx and the caller knows the cleanup is not durable.
            # Reporting partial counts on failure would let admins
            # believe stale out-of-scope embeddings were removed when
            # they were not — exactly the silent-failure mode the
            # purge endpoint exists to prevent.
            if daemon._backend is not None and txtai_ids_to_delete:
                deleted = int(
                    await daemon._backend.delete(txtai_ids_to_delete, zone_id=target_zone)
                )
                counts["txtai_docs"] += deleted

    logger.info(
        "Purged unscoped txtai artifacts: %d docs across %d zones "
        "(document_chunks preserved for rebuild)",
        counts["txtai_docs"],
        len(target_zones),
    )
    return counts

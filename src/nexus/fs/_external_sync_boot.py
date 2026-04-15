"""Lazy one-shot bootstrap for the external-CLI sync framework.

Runs AdapterRegistry.startup() at most once per process. Safe for the
slim nexus-fs wheel — all imports from nexus.bricks.auth.external_sync
are behind try/except ImportError guards.

Called by:
  - _auth_cli._try_profile_store_list()  (nexus-fs auth list)
  - _backend_factory._try_profile_store_select()  (S3 credential routing)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_sync_done = False


def ensure_external_sync() -> None:
    """Run external-CLI adapter sync once, populating the profile store.

    No-op after the first successful (or failed) call. All errors are
    swallowed — callers fall back to their existing behavior when the
    store is empty.
    """
    global _sync_done  # noqa: PLW0603
    if _sync_done:
        return
    _sync_done = True

    try:
        import asyncio

        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry
        from nexus.bricks.auth.profile_store import SqliteAuthProfileStore
        from nexus.fs._paths import persistent_dir
    except ImportError:
        # Slim wheel — external_sync not available. Silent no-op.
        return

    try:
        db_path = persistent_dir() / "auth_profiles.db"
        store = SqliteAuthProfileStore(db_path)
        try:
            registry = AdapterRegistry(
                adapters=[AwsCliSyncAdapter()],
                profile_store=store,
                startup_timeout=3.0,
            )
            coro = registry.startup()
            # If called inside an already-running event loop (e.g. async mount),
            # we can't use asyncio.run(). Fall back to running in a new thread.
            try:
                asyncio.get_running_loop()
                # Already in an async context — run sync in a thread
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(asyncio.run, coro).result(timeout=5.0)
            except RuntimeError:
                # No running loop — safe to use asyncio.run()
                asyncio.run(coro)
        finally:
            store.close()
    except Exception:
        logger.debug("External CLI sync failed during bootstrap", exc_info=True)

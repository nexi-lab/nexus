"""Lazy one-shot bootstrap for the external-CLI sync framework.

Runs AdapterRegistry.startup() at most once per process. Safe for the
slim nexus-fs wheel — all imports from nexus.bricks.auth.external_sync
use importlib.import_module() to avoid static import references that
would violate the packaging boundary (test_boundary.py).

Called by:
  - _auth_cli._try_profile_store_list()  (nexus-fs auth list)
  - _backend_factory._try_profile_store_select()  (S3 credential routing)
"""

from __future__ import annotations

import importlib
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

        aws_mod = importlib.import_module("nexus.bricks.auth.external_sync.aws_sync")
        reg_mod = importlib.import_module("nexus.bricks.auth.external_sync.registry")
        store_mod = importlib.import_module("nexus.bricks.auth.profile_store")
        from nexus.fs._paths import persistent_dir
    except (ImportError, ModuleNotFoundError):
        # Slim wheel — external_sync not available. Silent no-op.
        return

    try:
        db_path = persistent_dir() / "auth_profiles.db"
        store = store_mod.SqliteAuthProfileStore(db_path)
        try:
            registry = reg_mod.AdapterRegistry(
                adapters=[aws_mod.AwsCliSyncAdapter()],
                profile_store=store,
                startup_timeout=3.0,
            )
            coro = registry.startup()
            try:
                asyncio.get_running_loop()
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(asyncio.run, coro).result(timeout=5.0)
            except RuntimeError:
                asyncio.run(coro)
        finally:
            store.close()
    except Exception:
        logger.debug("External CLI sync failed during bootstrap", exc_info=True)


def list_profiles() -> list | None:
    """Read all profiles from the unified store. Returns None on any failure."""
    try:
        store_mod = importlib.import_module("nexus.bricks.auth.profile_store")
        from nexus.fs._paths import persistent_dir
    except (ImportError, ModuleNotFoundError):
        return None

    db_path = persistent_dir() / "auth_profiles.db"
    if not db_path.exists():
        return None

    try:
        store = store_mod.SqliteAuthProfileStore(db_path)
        try:
            profiles = store.list()
        finally:
            store.close()
        return profiles if profiles else None
    except Exception:
        return None


def select_profile(provider: str, *, account: str | None = None):  # noqa: ANN201
    """Select a usable profile for a provider. Returns None on any failure.

    When ``account`` is given, only that profile is considered. For S3,
    callers should pass ``os.environ.get("AWS_PROFILE")`` so the user's
    explicit account selection is honored. When multiple profiles exist
    and no account is specified, returns None (ambiguous — let the native
    provider chain decide).

    Returns None when all matching profiles are on cooldown or disabled,
    rather than forcing a known-broken profile back into service.
    """
    try:
        store_mod = importlib.import_module("nexus.bricks.auth.profile_store")
        from nexus.fs._paths import persistent_dir
    except (ImportError, ModuleNotFoundError):
        return None

    db_path = persistent_dir() / "auth_profiles.db"
    if not db_path.exists():
        return None

    try:
        store = store_mod.SqliteAuthProfileStore(db_path)
        try:
            profiles = store.list(provider=provider)
        finally:
            store.close()

        if not profiles:
            return None

        # Filter to explicit account if requested
        if account is not None:
            profiles = [p for p in profiles if p.account_identifier == account]
            if not profiles:
                return None

        # Ambiguous: multiple profiles and no account specified — let the
        # native provider chain decide instead of silently picking one.
        if len(profiles) > 1 and account is None:
            return None

        from datetime import UTC, datetime

        now = datetime.now(UTC)
        for profile in profiles:
            stats = profile.usage_stats
            if stats.cooldown_until and stats.cooldown_until > now:
                continue
            if stats.disabled_until and stats.disabled_until > now:
                continue
            return profile

        # All matching profiles are on cooldown/disabled — do not force a
        # known-broken profile back into service.
        return None
    except Exception:
        return None


def resolve_external_credential(backend_key: str):  # noqa: ANN201
    """Resolve a credential via AwsCliSyncAdapter. Returns None on failure."""
    try:
        import asyncio

        aws_mod = importlib.import_module("nexus.bricks.auth.external_sync.aws_sync")
    except (ImportError, ModuleNotFoundError):
        return None

    try:
        adapter = aws_mod.AwsCliSyncAdapter()
        coro = adapter.resolve_credential(backend_key)
        try:
            asyncio.get_running_loop()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=10.0)
        except RuntimeError:
            return asyncio.run(coro)
    except Exception:
        return None

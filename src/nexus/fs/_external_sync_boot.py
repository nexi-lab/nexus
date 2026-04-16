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
from typing import Any

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
        gcloud_mod = importlib.import_module("nexus.bricks.auth.external_sync.gcloud_sync")
        gh_mod = importlib.import_module("nexus.bricks.auth.external_sync.gh_sync")
        gws_mod = importlib.import_module("nexus.bricks.auth.external_sync.gws_sync")
        codex_mod = importlib.import_module("nexus.bricks.auth.external_sync.codex_sync")
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
                adapters=[
                    aws_mod.AwsCliSyncAdapter(),
                    gcloud_mod.GcloudSyncAdapter(),
                    gh_mod.GhCliSyncAdapter(),
                    gws_mod.GwsCliSyncAdapter(),
                    codex_mod.CodexSyncAdapter(),
                ],
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


def select_profile(provider: str, *, account: str | None = None) -> Any:
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

        # Remove blocked profiles (cooldown/disabled) before ambiguity check.
        # This ensures {default: blocked, work-prod: healthy} resolves to
        # work-prod rather than falling back to the native chain.
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        usable = [
            p
            for p in profiles
            if not (p.usage_stats.cooldown_until and p.usage_stats.cooldown_until > now)
            and not (p.usage_stats.disabled_until and p.usage_stats.disabled_until > now)
        ]

        if not usable:
            # All matching profiles are blocked — fail closed.
            return None

        # Ambiguous: multiple usable profiles and no account specified.
        if len(usable) > 1 and account is None:
            return None

        return usable[0]
    except Exception:
        return None


def resolve_token_for_provider(provider: str, *, account: str | None = None) -> str | None:
    """Select a profile for a provider and resolve its credential to a token.

    One-shot helper for sync contexts that need an access token or API key
    without plumbing a long-lived ``CredentialPoolRegistry`` through the app.
    Used by ``PathCLIBackend._resolve_from_external_cli`` (Phase 3, #3740).

    Args:
        provider: Unified provider name (e.g. ``"google"``, ``"github"``).
        account: Optional account identifier to filter by (user email,
            profile name). When omitted, uses the pool's default selection.

    Returns:
        A bearer access_token or api_key string, or ``None`` if no usable
        profile exists, no external-cli adapter can resolve it, or any step
        fails. Never raises.
    """
    profile = select_profile(provider, account=account)
    if profile is None or not getattr(profile, "backend_key", None):
        return None

    cred = resolve_external_credential(profile.backend_key)
    if cred is None:
        return None

    token = getattr(cred, "access_token", None) or getattr(cred, "api_key", None)
    return str(token) if token else None


def resolve_external_credential(backend_key: str) -> Any:
    """Resolve a credential by routing to the right adapter. Returns None on failure.

    Adapter selected from the ``{adapter_name}/...`` prefix of the backend_key:
    ``aws-cli``, ``gcloud``, ``gh-cli``, ``gws-cli``, ``codex``.
    """
    try:
        import asyncio

        aws_mod = importlib.import_module("nexus.bricks.auth.external_sync.aws_sync")
        gcloud_mod = importlib.import_module("nexus.bricks.auth.external_sync.gcloud_sync")
        gh_mod = importlib.import_module("nexus.bricks.auth.external_sync.gh_sync")
        gws_mod = importlib.import_module("nexus.bricks.auth.external_sync.gws_sync")
        codex_mod = importlib.import_module("nexus.bricks.auth.external_sync.codex_sync")
    except (ImportError, ModuleNotFoundError):
        return None

    adapter_name = backend_key.split("/", 1)[0] if "/" in backend_key else ""
    adapter_map = {
        "aws-cli": aws_mod.AwsCliSyncAdapter,
        "gcloud": gcloud_mod.GcloudSyncAdapter,
        "gh-cli": gh_mod.GhCliSyncAdapter,
        "gws-cli": gws_mod.GwsCliSyncAdapter,
        "codex": codex_mod.CodexSyncAdapter,
    }
    adapter_cls = adapter_map.get(adapter_name)
    if adapter_cls is None:
        return None

    try:
        adapter = adapter_cls()
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

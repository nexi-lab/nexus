"""Search startup — construct SearchDaemon and wire it to the Rust
``nexus-search-plugin`` cdylib, register VFS auto-index hooks so file
mutations refresh the plugin's index, and initialise the zone-search
registry used by federated search.

``NEXUS_SEARCH_DAEMON`` — default auto-enable when a database URL is
present; ``true`` forces on, ``false`` forces off.

``NEXUS_SEARCH_PLUGIN_TARGET`` — gRPC target the daemon dials
(defaults to the plugin's loopback bind).
"""

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


_TRUTHY = ("true", "1", "yes")
_FALSY = ("false", "0", "no")


async def startup_search(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Construct the Rust-backed SearchDaemon and wire it into the app."""
    _search_env = os.getenv("NEXUS_SEARCH_DAEMON", "").lower()
    _explicit_off = _search_env in _FALSY
    _explicit_on = _search_env in _TRUTHY
    search_daemon_enabled = _explicit_on or (not _explicit_off and bool(svc.database_url))

    if not search_daemon_enabled:
        logger.debug("Search Daemon disabled (set NEXUS_SEARCH_DAEMON=true to enable)")
        return []

    try:
        from nexus.bricks.search.rust_daemon import RustSearchDaemon

        app.state.search_daemon = RustSearchDaemon(
            target=os.environ.get("NEXUS_SEARCH_PLUGIN_TARGET"),
        )
        app.state.database_url = svc.database_url

        await app.state.search_daemon.startup()
        app.state.search_daemon_enabled = True

        # Wire the daemon into SearchService so semantic_search queries
        # route through the plugin instead of falling back to SQL ILIKE.
        with contextlib.suppress(AttributeError):
            search_svc = svc.nexus_fs.service("search")
            if search_svc is not None:
                search_svc._search_daemon = app.state.search_daemon

        _wire_notify_hooks(app, svc)
        _init_zone_registry(app, svc)

        logger.info(
            "Search Daemon started (backend=rust-plugin, target=%s)",
            os.environ.get("NEXUS_SEARCH_PLUGIN_TARGET") or "<default>",
        )

    except Exception as e:
        logger.warning("Failed to start Search Daemon: %s", e)
        app.state.search_daemon_enabled = False

    return []


async def shutdown_search(app: "FastAPI", svc: "LifespanServices") -> None:  # noqa: ARG001
    """Shut down the search daemon and release its resources."""
    daemon = getattr(app.state, "search_daemon", None)
    if daemon is None:
        return
    try:
        await daemon.shutdown()
        app.state.search_daemon_enabled = False
        logger.info("Search Daemon stopped")
    except Exception:
        app.state.search_daemon_enabled = False
        logger.warning("Search Daemon shutdown encountered errors", exc_info=True)


def _wire_notify_hooks(app: "FastAPI", svc: "LifespanServices") -> None:
    """Register VFS post-hooks that call ``notify_file_change`` on the daemon.

    VFS hooks fire from synchronous ``asyncio.to_thread`` contexts, so we
    capture the event loop at registration time and hand notifications back
    to it via ``call_soon_threadsafe`` (``get_running_loop`` would raise
    from the worker thread).
    """
    _nexus_fs = svc.nexus_fs
    if _nexus_fs is None or not hasattr(_nexus_fs, "register_intercept_write"):
        return

    _daemon_ref = app.state.search_daemon
    _loop = asyncio.get_running_loop()

    from nexus.contracts.vfs_hooks import (
        CopyHookContext,
        DeleteHookContext,
        RenameHookContext,
        WriteHookContext,
    )

    def _notify(path: str, change_type: str) -> None:
        with contextlib.suppress(RuntimeError):
            _loop.call_soon_threadsafe(
                _loop.create_task,
                _daemon_ref.notify_file_change(path, change_type),
            )

    class _SearchWriteHook:
        @property
        def name(self) -> str:
            return "search_auto_index"

        def on_post_write(self, ctx: WriteHookContext) -> None:
            _notify(ctx.path, "update")

    class _SearchDeleteHook:
        @property
        def name(self) -> str:
            return "search_auto_delete"

        def on_post_delete(self, ctx: DeleteHookContext) -> None:
            _notify(ctx.path, "delete")

    class _SearchRenameHook:
        @property
        def name(self) -> str:
            return "search_auto_rename"

        def on_post_rename(self, ctx: RenameHookContext) -> None:
            _notify(ctx.old_path, "delete")
            _notify(ctx.new_path, "update")

    class _SearchCopyHook:
        @property
        def name(self) -> str:
            return "search_auto_copy"

        def on_post_copy(self, ctx: CopyHookContext) -> None:
            _notify(ctx.dst_path, "update")

    _nexus_fs.register_intercept_write(_SearchWriteHook())
    _nexus_fs.register_intercept_delete(_SearchDeleteHook())
    _nexus_fs.register_intercept_rename(_SearchRenameHook())
    _nexus_fs.register_intercept_copy(_SearchCopyHook())
    logger.info("Search auto-index hooks registered (write/delete/rename/copy)")


def _init_zone_registry(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize ZoneSearchRegistry for federated search dispatch (Issue #3147).

    All zones share the single global daemon (the Rust plugin serves the
    local zone; cross-zone dispatch is a kernel concern).  When a
    ``zone_manager`` is available we also persist per-zone capabilities to
    ``{base_path}/{zone_id}/search_caps.json`` so the Rust
    ``GetSearchCapabilities`` gRPC handler can serve federation queries.
    """
    from nexus.bricks.search.zone_registry import ZoneSearchCapabilities, ZoneSearchRegistry

    daemon = app.state.search_daemon
    registry = ZoneSearchRegistry(default_daemon=daemon)

    zone_manager = getattr(svc, "zone_manager", None)
    if zone_manager is not None:
        try:
            zone_ids = zone_manager.list_zones()
            base_path = getattr(zone_manager, "_base_path", None)
            for zone_id in zone_ids:
                caps = ZoneSearchCapabilities.from_daemon_stats(zone_id, daemon)
                registry.register(zone_id, daemon, capabilities=caps)
                if base_path is not None:
                    _write_search_caps_file(base_path, zone_id, caps)
            logger.info(
                "[ZONE-REGISTRY] Registered %d zones from ZoneManager",
                len(zone_ids),
            )
        except Exception as e:
            logger.warning("[ZONE-REGISTRY] Failed to register zones: %s", e)

    app.state.zone_search_registry = registry


def _write_search_caps_file(base_path: str, zone_id: str, caps: object) -> None:
    """Atomically write per-zone search capabilities JSON (R20.12).

    Non-fatal on error — federation ``GetSearchCapabilities`` falls back
    to keyword-only defaults when the file is missing.
    """
    import json
    from pathlib import Path

    try:
        zone_dir = Path(base_path) / zone_id
        zone_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "device_tier": getattr(caps, "device_tier", "server"),
            "search_modes": list(getattr(caps, "search_modes", ["keyword"])),
            "embedding_model": getattr(caps, "embedding_model", None) or "",
            "embedding_dimensions": int(getattr(caps, "embedding_dimensions", 0) or 0),
            "has_graph": bool(getattr(caps, "has_graph", False)),
        }
        final_path = zone_dir / "search_caps.json"
        tmp_path = zone_dir / "search_caps.json.tmp"
        tmp_path.write_text(json.dumps(payload, indent=2))
        os.replace(tmp_path, final_path)
    except Exception as e:
        logger.warning("[ZONE-REGISTRY] Failed to write search_caps for %s: %s", zone_id, e)

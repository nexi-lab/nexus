"""SandboxBootstrapper — boot sequence for the sandbox profile (Issue #3786).

Sequence:
1. Create a local ``PathLocalBackend`` for the workspace directory.
2. Mount it in the Rust kernel under zone_id ``"local"`` via
   ``nexus_fs.sys_setattr("/zone/local", entry_type=DT_MOUNT, backend=..., zone_id="local")``.
3. Run ``FederationHandshake`` against the hub (if ``hub_url`` is provided).
   On ``HandshakeAuthError`` or ``HandshakeConnectionError`` log WARN and
   continue in local-only mode — never crash.
4. For each ``HubZoneGrant`` from the hub session: mount a Rust-native remote
   backend via ``nexus_fs.sys_setattr(f"/zone/{zone_id}", entry_type=DT_MOUNT,
   backend_type="remote", server_address=..., remote_auth_token=..., zone_id=...)``.
   This creates a live gRPC-backed mount; I/O routes to the hub automatically.
5. Register all zones in the ``ZoneSearchRegistry``:
   - ``"local"`` zone → ``search_registry.register(zone_id, search_daemon)``
   - remote zones → ``search_registry.register_remote(zone_id, transport)``
6. Start ``BootIndexer(workspace, search_daemon, health_state).start_async()``
   for background workspace indexing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nexus.backends.storage.path_local import PathLocalBackend
from nexus.contracts.exceptions import HandshakeAuthError, HandshakeConnectionError
from nexus.contracts.metadata import DT_MOUNT
from nexus.core.boot_indexer import BootIndexer
from nexus.remote.federation_handshake import FederationHandshake

logger = logging.getLogger(__name__)


class SandboxBootstrapper:
    """Orchestrates the sandbox profile boot sequence.

    Args:
        workspace:       Path to the local workspace directory (mounted as
                         the ``"local"`` read-write zone).
        hub_url:         gRPC URL of the remote Nexus hub to federate with,
                         or ``None`` to skip federation and run local-only.
        hub_token:       Bearer token presented to the hub during the
                         federation handshake.  May be ``None`` when
                         ``hub_url`` is ``None``.
        nexus_fs:        NexusFS instance used to mount zones into the Rust
                         kernel via ``sys_setattr``.
        search_registry: ``ZoneSearchRegistry`` (or equivalent).  Must expose
                         ``register(zone_id, daemon)`` for local zones and
                         ``register_remote(zone_id, transport)`` for remote ones.
        search_daemon:   Local search daemon instance passed to ``BootIndexer``
                         and registered as the daemon for the ``"local"`` zone.
        health_state:    Mutable dict with at least a ``"status"`` key.  Should
                         be ``{"status": "indexing"}`` before ``run()`` is called.
                         ``BootIndexer`` will transition it to ``"ready"`` once
                         the initial workspace walk completes.
    """

    def __init__(
        self,
        workspace: Path,
        hub_url: str | None,
        hub_token: str | None,
        nexus_fs: Any,
        search_registry: Any,
        search_daemon: Any,
        health_state: dict[str, Any],
    ) -> None:
        self._workspace = workspace
        self._hub_url = hub_url
        self._hub_token = hub_token
        self._nexus_fs = nexus_fs
        self._search_registry = search_registry
        self._search_daemon = search_daemon
        self._health_state = health_state

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the sandbox boot sequence (synchronous, called at startup)."""
        # Step 1 & 2: local zone — PathLocalBackend has root_path so Rust
        # constructs a path_local backend natively and owns the I/O path.
        local_backend = PathLocalBackend(self._workspace)
        self._nexus_fs.sys_setattr(
            "/zone/local",
            entry_type=DT_MOUNT,
            backend=local_backend,
            zone_id="root",  # ROOT_ZONE_ID so canonical key is /root/zone/local — matches
            # _build_rust_ctx which always supplies zone_id="root" for Python-bound calls
        )
        logger.info(
            "[SandboxBootstrapper] Mounted local zone: %s (canonical_key=/root/zone/local)",
            self._workspace,
        )

        if self._search_registry is not None:
            self._search_registry.register("local", self._search_daemon)

        # Step 3: Federation handshake
        hub_session = None
        if self._hub_url is not None:
            try:
                handshake = FederationHandshake(
                    hub_url=self._hub_url,
                    token=self._hub_token or "",
                )
                hub_session = handshake.run()
                logger.info(
                    "[SandboxBootstrapper] Hub handshake succeeded: %d zone grant(s)",
                    len(hub_session.zones),
                )
            except (HandshakeAuthError, HandshakeConnectionError) as exc:
                logger.warning(
                    "[SandboxBootstrapper] Hub handshake failed (%s: %s) — "
                    "continuing in local-only mode",
                    type(exc).__name__,
                    exc,
                )

        # Step 4 & 5: Remote zones — use Rust-native backend_type="remote" so
        # the kernel constructs a live gRPC client that routes I/O to the hub.
        # Strip grpc:// / grpcs:// prefix since server_address is host:port only.
        #
        # zone_id="root": the sandbox kernel's _build_rust_ctx always supplies
        # zone_id=ROOT_ZONE_ID ("root") for Python-bound calls, so the VFS
        # router canonicalises paths as "/root/zone/<id>/...". Registering
        # with zone_id="root" places the mount at "/root/zone/<id>" so the
        # router finds it. Using grant.zone_id here produced "/zone_id/zone/id"
        # which never matched and caused all remote-zone reads to return
        # FileNotFound (#3786).
        if hub_session is not None:
            _server_addr = (self._hub_url or "").removeprefix("grpc://").removeprefix("grpcs://")
            for grant in hub_session.zones:
                self._nexus_fs.sys_setattr(
                    f"/zone/{grant.zone_id}",
                    entry_type=DT_MOUNT,
                    backend_type="remote",
                    backend_name=f"remote_zone:{grant.zone_id}",
                    server_address=_server_addr,
                    remote_auth_token=self._hub_token or "",
                    zone_id="root",
                )
                logger.info(
                    "[SandboxBootstrapper] Mounted remote zone: %s (permission=%s, "
                    "canonical_key=/root/zone/%s, hub=%s)",
                    grant.zone_id,
                    grant.permission,
                    grant.zone_id,
                    _server_addr,
                )
                if self._search_registry is not None:
                    self._search_registry.register_remote(grant.zone_id, hub_session.transport)

        # Step 6: Background workspace indexing
        indexer = BootIndexer(
            workspace=self._workspace,
            search_daemon=self._search_daemon,
            health_state=self._health_state,
        )
        indexer.start_async()
        logger.info("[SandboxBootstrapper] BootIndexer started (background)")

"""FederationRPCService — residual Python surface for federation RPCs.

The bulk of the federation control plane (create / remove / join /
share / mount / unmount / list / cluster_info) now lives in the Rust
``services::federation::FederationService``.  Wire-form ``federation_*``
RPCs route through the gRPC tonic ``Call`` handler's
``resolve_rust_dispatch`` (``federation_`` prefix → Rust service)
BEFORE this Python service is consulted, so the 8 ported methods
never reach Python anymore.

What remains here:

  * ``federation_client_whoami`` — reflects the caller's auth-context
    grants. Stays Python until the tonic Call dispatch pipes auth
    context into Rust services (separate cross-cutting commit).

  * ``federation_export_zone`` / ``federation_import_zone`` — zone-
    bundle backup / migration utilities (NOT federation core logic).
    Tracked under task #49 to land as a separate
    ``services::portability`` Rust crate.

Once those three migrate, this whole file goes away alongside the
Python RPC envelope (task #45).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from nexus.contracts.exceptions import NexusPermissionError
from nexus.contracts.rpc import rpc_expose


class FederationRPCMixin:
    """Mixin providing auth-context-dependent federation RPC methods.

    Kept separate so the unit tests at ``tests/unit/grpc/`` can exercise
    ``federation_client_whoami`` without spinning up the full RPC
    service stack.
    """

    @rpc_expose(admin_only=False)
    def federation_client_whoami(self, _context: Any = None) -> dict[str, Any]:
        """Return the caller's zone grants for federation handshake.

        Called by thin clients during SandboxBootstrapper startup to discover
        which zones their bearer token can access and with what permissions.
        Returns a list of {zone_id, permission} dicts from the caller's context.

        Issue #3786: federation handshake for thin client.
        """
        if _context is None:
            raise NexusPermissionError("federation_client_whoami requires authentication")

        # P3-2 multi-zone tokens carry zone_perms: tuple[tuple[str, str], ...]
        zone_perms = getattr(_context, "zone_perms", None)
        if zone_perms:
            zones = [{"zone_id": zid, "permission": perm} for zid, perm in zone_perms]
            return {"zones": zones}

        # P3-1 single-zone tokens carry zone_id; look up actual permission if available.
        # Re-read zone_perms here: OperationContext.__post_init__ always populates it
        # from zone_id (defaulting to "rw"), so a real OperationContext will have a
        # non-empty zone_perms even when zone_id is set without an explicit zone_perms.
        # On raw/mock contexts without __post_init__, default to "rw" (legacy single-zone
        # tokens are write-capable per OperationContext.__post_init__ policy, #3785 F3c).
        zone_id = getattr(_context, "zone_id", None)
        if zone_id:
            perm = "rw"
            _zone_perms = getattr(_context, "zone_perms", None)
            if _zone_perms:
                for zid, p in _zone_perms:
                    if zid == zone_id:
                        perm = p
                        break
            return {"zones": [{"zone_id": zone_id, "permission": perm}]}

        raise NexusPermissionError("token carries no zone grants")


class FederationRPCService(FederationRPCMixin):
    """Residual Python service: zone export / import + whoami.

    Zone-create / remove / join / share / mount / unmount / list /
    cluster_info all land in ``services::federation::FederationService``
    (Rust) — the gRPC tonic Call handler routes wire-form
    ``federation_*`` RPCs to it via ``resolve_rust_dispatch``'s
    ``federation_`` prefix mapping. ``RustCallError::NotFound`` falls
    through to this Python service so ``federation_client_whoami`` /
    ``federation_export_zone`` / ``federation_import_zone`` keep
    working.
    """

    def __init__(self, kernel: Any, nexus_fs: Any = None) -> None:
        self._kernel = kernel
        self._nexus_fs = nexus_fs

    # ── Zone snapshot (export / import — task #49) ─────────────────

    @rpc_expose(admin_only=True)
    def federation_export_zone(
        self,
        zone_id: str,
        output_path: str,
        include_content: bool = True,
        include_permissions: bool = True,
        include_embeddings: bool = False,
        include_deleted: bool = False,
        path_prefix: str | None = None,
        sign: bool = False,
        strip_credentials: bool = False,
        after_time: str | None = None,
        before_time: str | None = None,
    ) -> dict[str, Any]:
        """Export a raft-backed zone to a .nexus bundle on the server's
        filesystem.

        Runs server-side so the exporter reaches the real metastore +
        backend rather than a remote proxy. CLI (``nexus zone export``,
        ``nexus archive create``) and the docker E2E suite call this RPC
        when the caller isn't the local owner of the redb file. The
        shared docker volume makes ``output_path`` visible to the CLI
        container; production deployments typically write to a mounted
        backup volume.

        The ``sign``/``strip_credentials``/``after_time``/``before_time``
        parameters drive the v2 archive features used by
        ``nexus archive create`` (#3793). Signing key is loaded from the
        server's ``~/.nexus/archive_signing_key`` (auto-generated on
        first use, TOFU trust model).
        """
        from datetime import datetime
        from pathlib import Path


        from nexus.bricks.portability import ZoneExportOptions, ZoneExportService

        nx = self._nexus_fs
        if nx is None:
            raise RuntimeError("federation_export_zone: no NexusFS attached")
        service = ZoneExportService(nx)
        options = ZoneExportOptions(
            output_path=Path(output_path),
            include_content=include_content,
            include_permissions=include_permissions,
            include_embeddings=include_embeddings,
            include_deleted=include_deleted,
            path_prefix=path_prefix,
            sign=sign,
            strip_credentials=strip_credentials,
            after_time=datetime.fromisoformat(after_time) if after_time else None,
            before_time=datetime.fromisoformat(before_time) if before_time else None,
        )
        manifest = service.export_zone(zone_id, options)
        return {
            "zone_id": zone_id,
            "output_path": str(options.output_path),
            "file_count": manifest.file_count,
            "total_size_bytes": manifest.total_size_bytes,
            "bundle_id": manifest.bundle_id,
        }

    @rpc_expose(admin_only=True)
    def federation_import_zone(
        self,
        bundle_path: str,
        target_zone: str | None = None,
        conflict: str = "skip",
        preserve_timestamps: bool = True,
        import_permissions: bool = True,
        path_prefix_remap: dict[str, str] | None = None,
        force: bool = False,
        rebuild_embeddings: bool = False,
        injections: dict[str, str] | None = None,
        require_no_placeholders: bool = True,
    ) -> dict[str, Any]:
        """Import a zone bundle from the server's filesystem.

        Server-side counterpart of ``federation_export_zone``. See that
        method's docstring for the shared-volume / mounted-backup
        deployment model.

        The ``force``/``rebuild_embeddings``/``injections``/
        ``require_no_placeholders`` parameters drive the v2 archive
        restore features used by ``nexus archive restore`` (#3793).
        """
        from nexus.bricks.portability import ConflictMode, ZoneImportOptions, ZoneImportService

        nx = self._nexus_fs
        if nx is None:
            raise RuntimeError("federation_import_zone: no NexusFS attached")
        # Create the raft zone before loading data into it.  Routes
        # through the standard sys_setattr DT_MOUNT surface (kernel
        # auto-creates via the federation provider when initialised).
        # Idempotent on already-exists.
        if target_zone:
            DT_MOUNT = 2
            with contextlib.suppress(Exception):
                self._kernel.sys_setattr(
                    "/__fed_import__",
                    1,  # DT_DIR
                    "",  # backend_name (unused for DT_DIR)
                )
            try:
                self._kernel.sys_setattr(
                    f"/__fed_import__/{target_zone}",
                    DT_MOUNT,
                    backend_name="",
                    zone_id=target_zone,
                )
            except Exception as e:
                if "already" not in str(e).lower() and "exists" not in str(e).lower():
                    raise
        service = ZoneImportService(nx)
        options = ZoneImportOptions(
            bundle_path=Path(bundle_path),
            target_zone_id=target_zone,
            conflict_mode=ConflictMode(conflict),
            preserve_timestamps=preserve_timestamps,
            import_permissions=import_permissions,
            path_prefix_remap=path_prefix_remap or {},
            force=force,
            rebuild_embeddings=rebuild_embeddings,
            injections=injections or {},
            require_no_placeholders=require_no_placeholders,
        )
        result = service.import_zone(options)
        return {
            "target_zone": target_zone,
            "files_created": result.files_created,
            "files_updated": result.files_updated,
            "files_skipped": result.files_skipped,
            "files_failed": result.files_failed,
            "permissions_imported": result.permissions_imported,
        }

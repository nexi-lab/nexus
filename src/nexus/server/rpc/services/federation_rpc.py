"""FederationRPCService — gRPC RPC surface for federation / zone ops.

R20.18.5 Phase B: the pre-R20.18.5 version was a Python wrapper around
the now-deleted ``ZoneManager`` shim. This rebuild delegates every
``federation_*`` RPC to the kernel's ``zone_*`` methods (tolerated
tech debt per v20.10 plan — the boundary-rule-compliant replacement
is the ``/__sys__/zones/`` procfs view reached through ``sys_stat``
/ ``sys_readdir``; tracked as a post-merge follow-up).

Registered in ``fastapi_server.create_app`` when federation is active
(kernel reports ``mount_reconciliation_done()`` once bootstrap finishes).
"""

from __future__ import annotations

import contextlib
from typing import Any

from nexus.contracts.rpc import rpc_expose


class FederationRPCService:
    """Federation CRUD RPCs backed by ``nexus_kernel`` zone_* methods."""

    def __init__(self, kernel: Any, nexus_fs: Any = None) -> None:
        self._kernel = kernel
        self._nexus_fs = nexus_fs

    def _register_mount_in_python_dlc(self, mount_path: str, parent_zone: str) -> None:
        """Rust kernel handles federation mount registration natively."""
        pass

    # ── Zone snapshot (export / import — R20.17b) ──────────────────

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
    ) -> dict[str, Any]:
        """Export a raft-backed zone to a .nexus bundle on the server's
        filesystem.

        Runs server-side so the exporter reaches the real metastore +
        backend rather than a remote proxy. CLI (``nexus zone export``)
        and the docker E2E suite call this RPC when the caller isn't
        the local owner of the redb file. The shared docker volume
        makes ``output_path`` visible to the CLI container; production
        deployments typically write to a mounted backup volume.
        """
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
    ) -> dict[str, Any]:
        """Import a zone bundle from the server's filesystem.

        Server-side counterpart of ``federation_export_zone``. See that
        method's docstring for the shared-volume / mounted-backup
        deployment model.
        """
        from pathlib import Path

        from nexus.bricks.portability import ConflictMode, ZoneImportOptions, ZoneImportService

        nx = self._nexus_fs
        if nx is None:
            raise RuntimeError("federation_import_zone: no NexusFS attached")
        # Create the raft zone before loading data into it. Import is a
        # restore-into-zone operation, not a create+restore, so the
        # target zone must exist on the raft side. Idempotent: a
        # pre-existing zone surfaces from `zone_create` as an "already
        # exists" error we swallow, keeping the RPC callable twice
        # with the same args.
        if target_zone:
            try:
                self._kernel.zone_create(target_zone)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise
        service = ZoneImportService(nx)
        options = ZoneImportOptions(
            bundle_path=Path(bundle_path),
            target_zone_id=target_zone,
            conflict_mode=ConflictMode(conflict),
            preserve_timestamps=preserve_timestamps,
            import_permissions=import_permissions,
            path_prefix_remap=path_prefix_remap or {},
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

    # ── Zone lifecycle ─────────────────────────────────────────────

    @rpc_expose(admin_only=True)
    def federation_create_zone(self, zone_id: str) -> dict[str, Any]:
        created = self._kernel.zone_create(zone_id)
        return {"zone_id": created}

    @rpc_expose(admin_only=True)
    def federation_remove_zone(self, zone_id: str, force: bool = False) -> dict[str, Any]:
        del force  # cascade unmount happens inside kernel apply-cb
        self._kernel.zone_remove(zone_id)
        return {"zone_id": zone_id, "removed": True}

    @rpc_expose(admin_only=True)
    def federation_join(
        self,
        peer_addr: str,
        remote_path: str,
        local_path: str,
    ) -> dict[str, Any]:
        """Join a zone advertised by a peer at `remote_path` and mount
        it locally at `local_path`.

        Discovery uses the raft-replicated share registry in the root
        zone — ``remote_path → zone_id`` is already on this node once
        the sharing node's ``federation_share`` commits. `peer_addr`
        is informational (accepted for CLI parity with ``nexusd
        join``); it's not contacted here because raft already mirrors
        the registry row to every cluster member.
        """
        del peer_addr  # reserved for out-of-cluster bootstrap; unused in raft mode

        zone_id = self._kernel.lookup_share(remote_path)
        if not zone_id:
            raise LookupError(
                f"No share registered for '{remote_path}'. "
                "The sharing node must call federation_share before federation_join."
            )

        # Join the zone's raft group locally (snapshot install happens
        # inside the kernel apply-cb wired by `install_federation_mount_coherence`).
        self._kernel.zone_join(zone_id)

        # Mount the shared zone at `local_path` so VFS routing reaches
        # it. Derive the local parent zone via sys_stat on the parent
        # directory (§12d: no route() exposure to service tier).
        _parent_dir = local_path.rsplit("/", 1)[0] or "/"
        _parent_stat = self._kernel.sys_stat(_parent_dir, "root")
        parent_zone = (
            (_parent_stat.get("zone_id") or "root")
            if _parent_stat and isinstance(_parent_stat, dict)
            else "root"
        )
        mount_result = self.federation_mount(
            parent_zone=parent_zone,
            path=local_path,
            target_zone=zone_id,
        )
        return {
            "zone_id": zone_id,
            "remote_path": remote_path,
            "local_path": local_path,
            "parent_zone": parent_zone,
            "mount": mount_result,
        }

    # ── Mount topology ─────────────────────────────────────────────

    def _to_zone_relative(self, parent_zone: str, path: str) -> str:
        """Translate a VFS-global path to the parent zone's namespace.

        For parent=root the zone key equals the global path.  For nested
        mounts (parent=corp, path=/corp/sales) corp's namespace is
        zone-relative (/sales); the Rust raft ``ZoneManager::mount``
        writes the DT_MOUNT entry at the key we pass, so we must strip
        the parent's own mount prefix first. Pre-R20.18.5 this lived in
        ``ZoneManager._resolve_mount_parent`` (Python).
        """
        if parent_zone == "root" or not path.startswith("/"):
            return path
        # Walk the kernel's zone_list to find where parent_zone is
        # globally mounted, then strip that prefix.
        try:
            zones = list(self._kernel.zone_list())
        except Exception:
            return path
        # Build a "target_zone -> global_path" map by scanning the Rust
        # mount table via get_mount_points (returns /zone/mount_point
        # canonical keys).  We don't have direct access, but
        # has_mount("/corp", "root") is the heuristic signal.  Easiest:
        # if path starts with "/<parent_zone>/" heuristically, strip it.
        del zones
        candidate_prefix = f"/{parent_zone}"
        if path == candidate_prefix:
            return "/"
        if path.startswith(candidate_prefix + "/"):
            return path[len(candidate_prefix) :]
        # Fallback: pass through — matches root semantics.
        return path

    @rpc_expose(admin_only=True)
    def federation_mount(
        self,
        parent_zone: str,
        path: str,
        target_zone: str,
    ) -> dict[str, Any]:
        """Mount ``target_zone`` at ``path`` (global VFS) inside
        ``parent_zone``. Uses the legacy kwarg shape (parent_zone /
        path / target_zone) expected by the docker E2E test suite +
        CLI callers.
        """
        zone_relative = self._to_zone_relative(parent_zone, path)
        self._kernel.zone_mount(parent_zone, zone_relative, target_zone)
        # Mirror into Python DLC at the VFS-global path so
        # service-tier callers can resolve the mount.
        self._register_mount_in_python_dlc(path, parent_zone)
        return {
            "parent_zone": parent_zone,
            "path": path,
            "target_zone": target_zone,
        }

    @rpc_expose(admin_only=True)
    def federation_unmount(
        self,
        parent_zone: str,
        path: str,
    ) -> dict[str, Any]:
        zone_relative = self._to_zone_relative(parent_zone, path)
        target = self._kernel.zone_unmount(parent_zone, zone_relative)
        # Mirror into Python DLC removal: without this the
        # unmounted path stays reachable via the mount-registered
        # path. Matches the pre-R20.18.5 unmount bookkeeping.
        nx = self._nexus_fs
        if nx is not None:
            coord = getattr(nx, "_driver_coordinator", None)
            if coord is not None:
                with contextlib.suppress(Exception):
                    coord.unmount(path, zone_id=parent_zone)
        return {"parent_zone": parent_zone, "path": path, "target_zone": target}

    @rpc_expose(admin_only=True)
    def federation_share(
        self,
        local_path: str,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish `local_path`'s subtree as a standalone federation zone.

        Shape matches ``FederationShareParams`` in
        ``_rpc_params_generated.py``. Server derives the parent zone +
        zone-relative prefix from the kernel routing table (mount LPM)
        so callers don't have to know where the path is mounted.

        On success, the new zone id is also written into the root zone's
        share registry (``/__shares__/<local_path>``) so peers can
        resolve it via their replicated root-zone state — no extra
        peer-discovery RPC required.

        Args:
            local_path: VFS-global path of the subtree to share.
            zone_id: Name for the newly-created zone. When omitted, a
                `share-{8-hex}` id is generated.

        Returns:
            ``{"zone_id", "parent_zone_id", "prefix", "entries_copied"}``.
        """
        import uuid

        # Derive parent_zone_id and prefix via sys_stat (§12d: no route()
        # exposure to service tier).  parent_zone_id is the zone that owns
        # the subtree; prefix is the zone-relative tail.
        _path_stat = self._kernel.sys_stat(local_path, "root")
        parent_zone_id = (
            (_path_stat.get("zone_id") or "root")
            if _path_stat and isinstance(_path_stat, dict)
            else "root"
        )
        # Find mount point by walking up to nearest DT_MOUNT ancestor.
        _mp = local_path
        while _mp != "/":
            _mp_stat = self._kernel.sys_stat(_mp, "root")
            if _mp_stat and isinstance(_mp_stat, dict) and _mp_stat.get("entry_type") == 2:
                break
            _mp = _mp.rsplit("/", 1)[0] or "/"
        _tail = local_path[len(_mp) :].lstrip("/") if _mp != local_path else ""
        prefix = "/" + _tail if _tail else "/"
        new_zone_id = zone_id or f"share-{uuid.uuid4().hex[:8]}"
        # Ensure the target zone exists BEFORE share_subtree_core runs
        # (it requires both parent and new zones to be loaded).
        self._kernel.zone_create(new_zone_id)
        copied = self._kernel.zone_share(parent_zone_id, prefix, new_zone_id)
        # Publish to the raft-replicated share registry so peers can
        # resolve `local_path → zone_id` without a separate RPC.
        self._kernel.register_share(local_path, new_zone_id)
        return {
            "zone_id": new_zone_id,
            "parent_zone_id": parent_zone_id,
            "prefix": prefix,
            "entries_copied": copied,
        }

    # ── Introspection ──────────────────────────────────────────────

    def _links_count(self, zone_id: str) -> int:
        fn = getattr(self._kernel, "zone_links_count", None)
        if fn is None:
            return 0
        try:
            return int(fn(zone_id))
        except Exception:
            return 0

    @rpc_expose(admin_only=False)
    def federation_list_zones(self) -> dict[str, Any]:
        zone_ids: list[str] = list(self._kernel.zone_list())
        zones = [{"zone_id": zid, "links_count": self._links_count(zid)} for zid in zone_ids]
        return {"zones": zones, "node_id": zone_ids}

    @rpc_expose(admin_only=False)
    def federation_cluster_info(self, zone_id: str) -> dict[str, Any]:
        # PyKernel.zone_cluster_info returns a Python dict already.
        status: dict[str, Any] = dict(self._kernel.zone_cluster_info(zone_id))
        status["links_count"] = self._links_count(zone_id)
        return status

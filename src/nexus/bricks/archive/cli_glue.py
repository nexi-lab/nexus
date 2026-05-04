"""Glue between Click commands and archive subsystems (#3793).

Kept thin so the CLI module stays free of nexus runtime imports until
invocation time (lazy imports inside each function).

Routing model:

* When ``remote_url`` (or the ambient ``NEXUS_URL`` env) resolves to a
  remote server, we dispatch through ``federation_export_zone`` /
  ``federation_import_zone`` /  ``federation_list_zones`` RPCs — the
  same pattern ``nexus zone export|import|list`` use. This avoids the
  CLI fighting for the redb lock the running server already holds.

* When no remote server is reachable, we fall back to an in-process
  ``ZoneExportService`` / ``ZoneImportService`` against a local-mode
  filesystem.  This is the offline-snapshot / disaster-recovery path.

Cross-brick note: this module imports from ``nexus.bricks.portability.*``
at call time. It is CLI glue (not a brick module) but lives in the
archive brick. The ``("archive", "portability")`` entry in
``KNOWN_CROSS_BRICK_EXCEPTIONS`` covers all files in
``nexus.bricks.archive`` (non-test).
"""

from __future__ import annotations

import json
import shutil
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def run_create(
    *,
    zone_ids: list[str] | None,
    output: Path,
    audit: bool,
    audit_from: datetime | None,
    audit_to: datetime | None,
    sign: bool,
    strip: bool,
    remote_url: str | None = None,
    remote_api_key: str | None = None,
) -> list[Any]:
    """Build one ``.nexus`` archive per zone.

    Routes through ``federation_export_zone`` RPC when a remote server is
    reachable; otherwise falls back to in-process ``ZoneExportService``.

    Args:
        zone_ids: Explicit list of zone IDs to archive, or ``None`` to
            export all zones (discovered via ``federation_list_zones``
            RPC when remote).
        output: Destination *directory* for the generated ``.nexus`` files.
        audit: When ``True``, restrict each bundle to the audit time
            window defined by *audit_from* / *audit_to*.
        audit_from: Lower bound of audit window (inclusive). Only used
            when *audit* is ``True``.
        audit_to: Upper bound of audit window (inclusive). Only used
            when *audit* is ``True``.
        sign: Sign each bundle with the operator's ed25519 key.
        strip: Strip credential placeholders before writing each bundle.
        remote_url: Override remote URL (else ``NEXUS_URL`` env).
        remote_api_key: Override remote API key (else ``NEXUS_API_KEY``).

    Returns:
        List of per-zone result dicts (or ExportManifest when local).
    """
    from nexus.cli.config import resolve_connection

    resolved = resolve_connection(remote_url=remote_url, remote_api_key=remote_api_key)

    after = audit_from if audit else None
    before = audit_to if audit else None

    if resolved.is_remote:
        # Server-side write — ``output`` lives on the server's
        # filesystem and is treated as a *directory*. Per-zone files
        # land at ``output/<zone>-<utc-iso>.nexus``. We can't probe
        # ``is_dir()`` here because the host may not see the
        # container's mount layout, and we deliberately do NOT mkdir
        # on the host — the path must be reachable in the server
        # container (e.g. via a shared volume or the data-dir bind
        # mount).
        assert resolved.url is not None  # is_remote ⇒ url present
        return _run_create_remote(
            resolved.url,
            resolved.api_key,
            zone_ids=zone_ids,
            output_dir=output,
            sign=sign,
            strip=strip,
            after=after,
            before=before,
        )

    local_output_dir = output if output.is_dir() else output.parent
    local_output_dir.mkdir(parents=True, exist_ok=True)
    return _run_create_local(
        zone_ids=zone_ids,
        output_dir=local_output_dir,
        sign=sign,
        strip=strip,
        after=after,
        before=before,
    )


def run_restore(
    *,
    file: Path,
    target_zone: str | None,
    require_trusted: bool,
    rebuild_embeddings: bool,
    force: bool,
    injections: dict[str, str],
    remote_url: str | None = None,
    remote_api_key: str | None = None,
) -> None:
    """Verify, optionally TOFU-check, then restore a ``.nexus`` bundle.

    Steps:

    1. ``verify_archive(file, strict=True)`` — signature + version gate.
    2. If *require_trusted*, check signer pubkey against the TOFU trust
       store at ``~/.nexus/trusted_signers.json``.
    3. Dispatch ``federation_import_zone`` RPC when remote; otherwise
       open a local-mode filesystem and call ``ZoneImportService``.
    4. Print a federation re-pair list for any federations that will need
       re-authentication after the restore.

    Args:
        file: Path to the ``.nexus`` bundle to restore.
        target_zone: Remap the restored zone to this ID, or ``None`` to
            keep the original zone ID from the bundle.
        require_trusted: When ``True``, abort if the bundle signer is not
            in the TOFU trust store.
        rebuild_embeddings: Pass-through to ``ZoneImportOptions``; when
            ``True`` embeddings will be re-built after restore.
        force: Allow restore into a non-empty target (DESTRUCTIVE).
        injections: Mapping of placeholder name → value to inject before
            restore (replaces ``${NAME}`` tokens in credential fields).
        remote_url: Override remote URL (else ``NEXUS_URL`` env).
        remote_api_key: Override remote API key (else ``NEXUS_API_KEY``).

    Raises:
        ArchiveSignatureError: Signature verification failed.
        ArchiveVersionIncompatible: Bundle requires a newer nexus version.
        ArchiveUntrustedSigner: Signer not in trust store when
            *require_trusted* is ``True``.
        ArchiveTargetNotEmpty: Target has existing zones and *force* is
            ``False``.
    """
    from nexus.bricks.archive.errors import ArchiveUntrustedSigner
    from nexus.bricks.archive.verify import verify_archive
    from nexus.bricks.portability.trust import TrustStore
    from nexus.cli.config import resolve_connection

    # Local verify only when the file is reachable on this host. In
    # remote mode the bundle may live on the server (e.g. inside the
    # container at ``/app/data/archives/foo.nexus`` — the same path
    # that ``archive create`` writes to). When it isn't visible
    # locally we let the server perform verification during import.
    host_visible = file.exists()
    if host_visible:
        verify_archive(file, strict=True)

        if require_trusted:
            with tarfile.open(file, "r:gz") as tar:
                sig_member = tar.extractfile("signatures.json")
                assert sig_member is not None
                sig_doc = json.loads(sig_member.read())
            store = TrustStore(Path.home() / ".nexus" / "trusted_signers.json")
            pubkey = sig_doc["signer_pubkey_b64"]
            if not store.is_trusted(pubkey):
                raise ArchiveUntrustedSigner(pubkey)
    elif require_trusted:
        raise ArchiveUntrustedSigner(
            "--require-trusted needs the bundle to be readable on the CLI host "
            "(make the archive path visible via a shared volume, or omit the flag "
            "to defer trust enforcement to the server)."
        )

    resolved = resolve_connection(remote_url=remote_url, remote_api_key=remote_api_key)
    if resolved.is_remote:
        assert resolved.url is not None  # is_remote ⇒ url present
        _run_restore_remote(
            resolved.url,
            resolved.api_key,
            file=file,
            target_zone=target_zone,
            rebuild_embeddings=rebuild_embeddings,
            force=force,
            injections=injections,
        )
        return

    _run_restore_local(
        file=file,
        target_zone=target_zone,
        rebuild_embeddings=rebuild_embeddings,
        force=force,
        injections=injections,
    )


def run_keys_rotate() -> str:
    """Rotate the archive signing keypair.

    Backs up the current key to a timestamped ``.bak`` file, then
    generates a fresh ed25519 keypair at ``~/.nexus/archive_signing_key``.

    Returns:
        The new signer public key as a base64 string.
    """
    from nexus.bricks.portability.signer import ArchiveSigner

    key_path = Path.home() / ".nexus" / "archive_signing_key"
    if key_path.exists():
        backup = key_path.with_name(f"archive_signing_key.{int(time.time())}.bak")
        shutil.move(str(key_path), str(backup))
        pub = key_path.with_suffix(".pub")
        if pub.exists():
            shutil.move(str(pub), str(backup) + ".pub")

    signer = ArchiveSigner(key_path)
    return signer.public_key_b64


# ---------------------------------------------------------------------------
# Remote (RPC) dispatch
# ---------------------------------------------------------------------------


def _run_create_remote(
    remote_url: str,
    remote_api_key: str | None,
    *,
    zone_ids: list[str] | None,
    output_dir: Path,
    sign: bool,
    strip: bool,
    after: datetime | None,
    before: datetime | None,
) -> list[Any]:
    """Drive ``federation_export_zone`` once per zone via RPC."""
    from datetime import UTC
    from datetime import datetime as _dt

    from nexus.cli.utils import rpc_call

    if zone_ids is None:
        listing = rpc_call(remote_url, remote_api_key, "federation_list_zones")
        zone_ids = [z["zone_id"] for z in listing.get("zones", [])]

    ts = _dt.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results: list[Any] = []
    for zid in zone_ids:
        out_path = output_dir / f"{zid}-{ts}.nexus"
        data = rpc_call(
            remote_url,
            remote_api_key,
            "federation_export_zone",
            zone_id=zid,
            output_path=str(out_path),
            sign=sign,
            strip_credentials=strip,
            after_time=after.isoformat() if after else None,
            before_time=before.isoformat() if before else None,
        )
        results.append(data)
    return results


def _run_restore_remote(
    remote_url: str,
    remote_api_key: str | None,
    *,
    file: Path,
    target_zone: str | None,
    rebuild_embeddings: bool,
    force: bool,
    injections: dict[str, str],
) -> None:
    """Drive ``federation_import_zone`` via RPC.

    Sends ``file`` verbatim — the path must be reachable on the
    *server*, not necessarily on the CLI host. ``file.resolve()``
    would mangle a server-only path (e.g. ``/app/data/foo.nexus``)
    into a host-rooted absolute, so we only resolve when the bundle
    actually exists on this host.
    """
    from nexus.cli.utils import rpc_call

    bundle_path = str(file.resolve()) if file.exists() else str(file)
    rpc_call(
        remote_url,
        remote_api_key,
        "federation_import_zone",
        bundle_path=bundle_path,
        target_zone=target_zone,
        force=force,
        rebuild_embeddings=rebuild_embeddings,
        injections=injections or None,
    )


# ---------------------------------------------------------------------------
# Local (in-process) fallback
# ---------------------------------------------------------------------------


def _run_create_local(
    *,
    zone_ids: list[str] | None,
    output_dir: Path,
    sign: bool,
    strip: bool,
    after: datetime | None,
    before: datetime | None,
) -> list[Any]:
    """Open a local NexusFS and run the orchestrator in-process."""
    from nexus.bricks.archive.orchestrator import ArchiveOrchestrator
    from nexus.bricks.portability.export_service import ZoneExportService

    nexus_fs = _open_nexus_fs()
    export_service = ZoneExportService(nexus_fs)
    orch = ArchiveOrchestrator(
        export_service=export_service,
        output_dir=output_dir,
        zone_lister=lambda: _list_zones(nexus_fs),
    )
    return orch.create_archives(
        zone_ids=zone_ids,
        strip=strip,
        sign=sign,
        audit_from=after,
        audit_to=before,
    )


def _run_restore_local(
    *,
    file: Path,
    target_zone: str | None,
    rebuild_embeddings: bool,
    force: bool,
    injections: dict[str, str],
) -> None:
    """Open a local NexusFS and import the bundle in-process."""
    from nexus.bricks.portability.import_service import ZoneImportService
    from nexus.bricks.portability.models import ZoneImportOptions

    nexus_fs = _open_nexus_fs()
    import_service = ZoneImportService(nexus_fs)
    options = ZoneImportOptions(
        bundle_path=file,
        target_zone_id=target_zone,
        force=force,
        rebuild_embeddings=rebuild_embeddings,
        injections=injections,
    )
    import_service.import_zone(options)
    _print_federation_repair_list(nexus_fs)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _open_nexus_fs() -> Any:
    """Locate the running nexus filesystem instance for CLI use.

    Lazy-imports ``nexus.cli.utils.get_filesystem`` to avoid pulling the
    entire runtime when the CLI is invoked with ``--help``. The upstream
    ``get_filesystem`` is async; we drive it via ``asyncio.run`` so the
    glue presents a synchronous interface to the Click CLI.

    Returns:
        The active nexus filesystem handle.
    """
    import asyncio
    import importlib

    cli_utils = importlib.import_module("nexus.cli.utils")
    return asyncio.run(cli_utils.get_filesystem(allow_local_default=True))


def _list_zones(nexus_fs: Any) -> list[str]:
    """Return a list of zone IDs from *nexus_fs*.

    Reads the kernel ``/__sys__/zones/`` procfs view served by the local
    Rust runtime. The legacy ``nexus_fs.metadata.list_zones()`` path on
    the Python proxy is gone (commit V dropped the Raft-only methods).

    Args:
        nexus_fs: Active nexus filesystem handle.

    Returns:
        List of zone ID strings (may be empty if the kernel can't be
        reached).
    """
    try:
        kernel = getattr(nexus_fs, "_kernel", None) or getattr(nexus_fs, "kernel", None)
        if kernel is not None:
            return list(kernel.sys_readdir_backend("/__sys__/zones/", "root"))
    except Exception:
        pass
    return []


def _print_federation_repair_list(nexus_fs: Any) -> None:  # noqa: ARG001 — unused after V
    """Print federation re-pair commands to stdout after a restore.

    Federations lose their auth tokens during a restore because the tokens
    are redacted in the bundle.  This helper surfaces the commands the
    operator must run to re-authenticate.

    Args:
        nexus_fs: Active nexus filesystem handle.

    Notes:
        Uses ``rich.Console`` for output. ``list_federations`` was a
        Raft-only method that V dropped from the Python boundary; the
        repair-list helper is now an unconditional no-op until the
        federation registry resurfaces on the kernel side.
    """
    from rich.console import Console

    console = Console()
    federations: list[Any] = []

    if not federations:
        return

    console.print("[bold]Federation re-pair required:[/]")
    for fed in federations:
        console.print(f"  nexus federation auth {fed.url}")


__all__ = [
    "_list_zones",
    "_open_nexus_fs",
    "_print_federation_repair_list",
    "run_create",
    "run_keys_rotate",
    "run_restore",
]

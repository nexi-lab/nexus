"""Glue between Click commands and archive subsystems (#3793).

Kept thin so the CLI module stays free of nexus runtime imports until
invocation time (lazy imports inside each function).

Cross-brick note: ``run_restore`` imports from ``nexus.bricks.portability.*``
at call time — this is CLI glue, not a brick module, but is housed in the
archive brick.  The ``("archive", "portability")`` entry in
``KNOWN_CROSS_BRICK_EXCEPTIONS`` covers all files in ``nexus.bricks.archive``
(non-test).
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
) -> list[Any]:
    """Wire up orchestrator + export service; build one archive per zone.

    Opens the running nexus filesystem, instantiates ``ZoneExportService``
    and ``ArchiveOrchestrator``, then delegates to
    ``ArchiveOrchestrator.create_archives()``.

    Args:
        zone_ids: Explicit list of zone IDs to archive, or ``None`` to
            export all zones discovered via ``nexus_fs.metadata.list_zones()``.
        output: Destination *directory* for the generated ``.nexus`` files.
        audit: When ``True``, restrict each bundle to the audit time window
            defined by *audit_from* / *audit_to*.
        audit_from: Lower bound of audit window (inclusive). Only used when
            *audit* is ``True``.
        audit_to: Upper bound of audit window (inclusive). Only used when
            *audit* is ``True``.
        sign: Sign each bundle with the operator's ed25519 key.
        strip: Strip credential placeholders before writing each bundle.

    Returns:
        List of ``ExportManifest`` instances returned by the orchestrator
        (one per exported zone).
    """
    from nexus.bricks.archive.orchestrator import ArchiveOrchestrator
    from nexus.bricks.portability.export_service import ZoneExportService

    nexus_fs = _open_nexus_fs()
    export_service = ZoneExportService(nexus_fs)
    orch = ArchiveOrchestrator(
        export_service=export_service,
        output_dir=output if output.is_dir() else output.parent,
        zone_lister=lambda: _list_zones(nexus_fs),
    )
    return orch.create_archives(
        zone_ids=zone_ids,
        strip=strip,
        sign=sign,
        audit_from=audit_from if audit else None,
        audit_to=audit_to if audit else None,
    )


def run_restore(
    *,
    file: Path,
    target_zone: str | None,
    require_trusted: bool,
    rebuild_embeddings: bool,
    force: bool,
    injections: dict[str, str],
) -> None:
    """Verify, optionally TOFU-check, then restore a ``.nexus`` bundle.

    Steps:

    1. ``verify_archive(file, strict=True)`` — signature + version gate.
    2. If *require_trusted*, check signer pubkey against the TOFU trust
       store at ``~/.nexus/trusted_signers.json``.
    3. Open the nexus filesystem and import the zone via
       ``ZoneImportService.import_zone()``.
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
    from nexus.bricks.portability.import_service import ZoneImportService
    from nexus.bricks.portability.models import ZoneImportOptions
    from nexus.bricks.portability.trust import TrustStore

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
# Private helpers
# ---------------------------------------------------------------------------


def _open_nexus_fs() -> Any:
    """Locate the running nexus filesystem instance for CLI use.

    Lazy-imports ``nexus.cli.utils.get_filesystem`` to avoid pulling the
    entire runtime when the CLI is invoked with ``--help``.

    Returns:
        The active nexus filesystem handle.
    """
    import importlib

    cli_utils = importlib.import_module("nexus.cli.utils")
    return cli_utils.get_filesystem()


def _list_zones(nexus_fs: Any) -> list[str]:
    """Return a list of zone IDs from *nexus_fs*.

    Calls ``nexus_fs.metadata.list_zones()``.  Wraps in try/except
    AttributeError in case the backend does not expose this method.

    Args:
        nexus_fs: Active nexus filesystem handle.

    Returns:
        List of zone ID strings (may be empty if the method is absent).
    """
    try:
        return [z.zone_id for z in nexus_fs.metadata.list_zones()]
    except AttributeError:
        return []


def _print_federation_repair_list(nexus_fs: Any) -> None:
    """Print federation re-pair commands to stdout after a restore.

    Federations lose their auth tokens during a restore because the tokens
    are redacted in the bundle.  This helper surfaces the commands the
    operator must run to re-authenticate.

    Args:
        nexus_fs: Active nexus filesystem handle.

    Notes:
        Uses ``rich.Console`` for output.  If
        ``nexus_fs.metadata.list_federations()`` raises ``AttributeError``
        (backend doesn't support it) or returns an empty list, the function
        is a no-op.
    """
    from rich.console import Console

    console = Console()
    federations: list[Any] = []
    try:
        federations = nexus_fs.metadata.list_federations()
    except AttributeError:
        return

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

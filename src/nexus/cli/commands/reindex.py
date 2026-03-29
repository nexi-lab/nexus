"""Reindex CLI command — replay operation_log MCL to rebuild indices (Issue #2929).

Consumes OperationLogModel entries via ``OperationLogger.replay_changes()``
to rebuild aspect store state (Key Decision #2: MCL in existing operation_log,
not a third event system). Supports incremental and clean modes with
cursor-based batching and checkpoint resume.

Targets:
    - search: Rebuild aspect store (version-0 state) from MCL events
    - versions: Rebuild full version history from MCL events
    - semantic: Re-extract schemas from filesystem via CatalogService
    - all: Run all targets (search + versions + semantic)

Examples:
    nexus reindex --target search
    nexus reindex --target all --dry-run
    nexus reindex --target semantic --zone z1
    nexus reindex --target search --from-sequence 1000 --batch-size 200
"""

import json
import logging
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models.operation_log import OperationLogModel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from nexus.cli.utils import (
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)

logger = logging.getLogger(__name__)


def register_commands(cli: click.Group) -> None:
    """Register reindex command with the CLI."""
    cli.add_command(reindex)


@click.command(name="reindex")
@click.option(
    "--target",
    "-t",
    type=click.Choice(["search", "versions", "semantic", "all"]),
    default="all",
    help="Index target to rebuild",
)
@click.option("--dry-run", is_flag=True, help="Show what would be reindexed without making changes")
@click.option(
    "--from-sequence",
    type=int,
    default=None,
    help="Resume from this MCL sequence number (inclusive)",
)
@click.option("--batch-size", type=int, default=500, help="Number of MCL records per batch")
@click.option("--zone", "-z", type=str, default=None, help="Filter by zone ID")
@add_backend_options
def reindex(
    target: str,
    dry_run: bool,
    from_sequence: int | None,
    batch_size: int,
    zone: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Replay operation_log to rebuild indices.

    Uses OperationLogger.replay_changes() to iterate operation_log records
    that carry MCL columns (entity_urn IS NOT NULL) and apply them to the
    aspect store. Each record is replayed idempotently: upserts overwrite
    current state, deletes soft-delete aspects.

    Examples:
        nexus reindex --target search
        nexus reindex --target all --dry-run
        nexus reindex --target search --from-sequence 1000
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)

        record_store = getattr(nx, "_record_store", None)
        if record_store is None:
            # Fall back to REST API for remote presets (shared/demo)
            _reindex_via_rest(
                remote_url=remote_url,
                remote_api_key=remote_api_key,
                target=target,
                dry_run=dry_run,
                from_sequence=from_sequence,
                batch_size=batch_size,
            )
            nx.close()
            return

        from sqlalchemy import func, select

        from nexus.storage.models.operation_log import OperationLogModel
        from nexus.storage.operation_logger import OperationLogger

        effective_targets = {"search", "versions", "semantic"} if target == "all" else {target}

        with record_store.session_factory() as session:
            # --- Semantic reindex: filesystem walk + CatalogService ---
            if "semantic" in effective_targets:
                _run_semantic_reindex(nx, session, zone, dry_run)

            # --- MCL-based targets (search, versions) ---
            mcl_targets = effective_targets - {"semantic"}
            if mcl_targets:
                mcl_target = "all" if mcl_targets == {"search", "versions"} else mcl_targets.pop()

                # Count operation_log rows with MCL columns for progress bar
                count_stmt = (
                    select(func.count())
                    .select_from(OperationLogModel)
                    .where(OperationLogModel.entity_urn.isnot(None))
                )
                if from_sequence is not None:
                    count_stmt = count_stmt.where(
                        OperationLogModel.sequence_number >= from_sequence
                    )
                if zone is not None:
                    count_stmt = count_stmt.where(OperationLogModel.zone_id == zone)

                total = session.execute(count_stmt).scalar_one()

                if total == 0:
                    console.print("[nexus.warning]No MCL records to process[/nexus.warning]")
                elif dry_run:
                    _show_dry_run_summary(total, mcl_target)
                else:
                    # Replay via OperationLogger.replay_changes()
                    op_logger = OperationLogger(session)
                    processor = _MCLProcessor(session, mcl_target)
                    processed = 0
                    last_sequence = from_sequence or 0
                    errors = 0

                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        console=console,
                    ) as progress:
                        task = progress.add_task(f"Reindexing {mcl_target}...", total=total)

                        for row in op_logger.replay_changes(
                            from_sequence=from_sequence or 0,
                            zone_id=zone,
                            batch_size=batch_size,
                        ):
                            try:
                                processor.process(row)
                                processed += 1
                                last_sequence = row.sequence_number
                            except Exception as e:
                                errors += 1
                                console.print(
                                    f"[nexus.error]Error at seq {row.sequence_number}: {e}[/nexus.error]"
                                )

                            progress.update(task, advance=1)

                    session.commit()

                    # Summary
                    console.print()
                    table = Table(title="Reindex Summary")
                    table.add_column("Metric", style="nexus.value")
                    table.add_column("Value", style="nexus.success")
                    table.add_row("Target", mcl_target)
                    table.add_row("Total records", str(total))
                    table.add_row("Processed", str(processed))
                    table.add_row("Errors", str(errors))
                    table.add_row("Last sequence", str(last_sequence))
                    console.print(table)

                    if errors > 0:
                        console.print(
                            f"\n[nexus.warning]Resume from sequence {last_sequence + 1} to retry:[/nexus.warning]"
                        )
                        console.print(
                            f"  nexus reindex --target {mcl_target} "
                            f"--from-sequence {last_sequence + 1}"
                        )

        nx.close()

    except Exception as e:
        handle_error(e)


def _reindex_via_rest(
    *,
    remote_url: str | None,
    remote_api_key: str | None,
    target: str,
    dry_run: bool,
    from_sequence: int | None,
    batch_size: int,
) -> None:
    """Run reindex via REST API for remote presets (shared/demo).

    The REST endpoint supports search and versions targets via MCL replay.
    Semantic reindex requires local filesystem access and is not supported remotely.
    """
    if target == "semantic":
        raise click.ClickException(
            "Semantic reindex requires local filesystem access. "
            "Use 'nexus reindex --target semantic' with a local RecordStore."
        )

    from nexus.cli.api_client import get_api_client_from_options

    client = get_api_client_from_options(remote_url, remote_api_key)
    try:
        result = client.post(
            "/api/v2/admin/reindex",
            json_body={
                "target": target if target != "all" else "all",
                "dry_run": dry_run,
                "from_sequence": from_sequence,
                "batch_size": batch_size,
            },
        )
    except Exception as e:
        raise click.ClickException(
            f"Reindex requires either a local RecordStore or a running REST API. "
            f"REST API error: {e}"
        ) from e

    # Display result
    console.print()
    table = Table(title="Reindex Summary (via REST API)")
    table.add_column("Metric", style="nexus.value")
    table.add_column("Value", style="nexus.success")
    table.add_row("Target", result.get("target", target))
    table.add_row("Total records", str(result.get("total", 0)))
    table.add_row("Processed", str(result.get("processed", 0)))
    table.add_row("Errors", str(result.get("errors", 0)))
    table.add_row("Dry run", str(result.get("dry_run", dry_run)))
    if target == "all":
        console.print(
            "\n[nexus.warning]Note:[/nexus.warning] Semantic reindex requires local filesystem access "
            "and was skipped. Only search + versions targets were processed."
        )
    console.print(table)


def _run_semantic_reindex(
    nx: Any,
    session: "Session",
    zone_id: str | None,
    dry_run: bool,
) -> None:
    """Walk the filesystem and re-extract schemas + document structure.

    For each file, computes its URN, reads content, and runs
    CatalogService.extract_auto() to rebuild schema_metadata or
    document_structure aspects (Issue #2978).
    """
    import mimetypes

    from nexus.bricks.catalog.protocol import CatalogService
    from nexus.storage.aspect_service import AspectService

    aspect_service = AspectService(session)
    catalog = CatalogService(aspect_service)

    # Walk the filesystem to discover files
    try:
        all_files = _walk_filesystem(nx, "/")
    except Exception as e:
        console.print(f"[nexus.error]Failed to walk filesystem: {e}[/nexus.error]")
        return

    if dry_run:
        console.print("\n[bold nexus.value]Dry Run — semantic reindex[/bold nexus.value]\n")
        console.print(f"Total files to process: [bold]{len(all_files)}[/bold]")
        console.print("\n[nexus.muted]Run without --dry-run to execute.[/nexus.muted]")
        return

    processed = 0
    schemas_extracted = 0
    documents_extracted = 0
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Semantic reindex...", total=len(all_files))

        for file_path in all_files:
            try:
                from nexus.contracts.urn import NexusURN

                urn = str(NexusURN.for_file(zone_id or "default", file_path))
                filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
                mime_type, _ = mimetypes.guess_type(file_path)

                # Format-gate: skip files with no extractor (Issue #2978)
                if not catalog.has_extractor(mime_type=mime_type, filename=filename):
                    processed += 1
                    progress.update(task, advance=1)
                    continue

                # Read file content via NexusFS (CAS-backed).
                # NOTE: extract_from_path() exists for external callers with
                # real OS paths, but NexusFS.physical_path is a CAS content
                # hash, not a filesystem path. Reindex always reads full
                # content; the 100MB size gate in CatalogService protects
                # against oversized files.
                content = nx.read(file_path)
                if isinstance(content, str):
                    content = content.encode()

                result = catalog.extract_auto(
                    entity_urn=urn,
                    content=content,
                    mime_type=mime_type,
                    filename=filename,
                    zone_id=zone_id,
                    created_by="reindex",
                )

                processed += 1

                # Track what was extracted
                from nexus.bricks.catalog.extractors import DocumentExtractionResult

                if isinstance(result, DocumentExtractionResult):
                    if result.error is None:
                        documents_extracted += 1
                elif hasattr(result, "schema") and result.schema is not None:
                    schemas_extracted += 1

            except Exception as e:
                errors += 1
                logger.debug("Semantic reindex failed for %s: %s", file_path, e)

            progress.update(task, advance=1)

    session.commit()

    # Summary
    console.print()
    table = Table(title="Semantic Reindex Summary")
    table.add_column("Metric", style="nexus.value")
    table.add_column("Value", style="nexus.success")
    table.add_row("Total files", str(len(all_files)))
    table.add_row("Processed", str(processed))
    table.add_row("Schemas extracted", str(schemas_extracted))
    table.add_row("Documents extracted", str(documents_extracted))
    table.add_row("Errors", str(errors))
    console.print(table)


def _walk_filesystem(nx: Any, root: str) -> list[str]:
    """Recursively walk NexusFS and return all file paths."""
    files: list[str] = []
    try:
        entries = nx.listdir(root)
    except Exception:
        return files

    for entry in entries:
        full_path = f"{root.rstrip('/')}/{entry}" if root != "/" else f"/{entry}"
        try:
            stat = nx.stat(full_path)
            if hasattr(stat, "is_dir") and stat.is_dir:
                files.extend(_walk_filesystem(nx, full_path))
            else:
                files.append(full_path)
        except Exception:
            files.append(full_path)

    return files


def _show_dry_run_summary(
    total: int,
    target: str,
) -> None:
    """Show a summary of what would be reindexed."""
    console.print(f"\n[bold nexus.value]Dry Run — {target} reindex[/bold nexus.value]\n")
    console.print(f"Total MCL records to process: [bold]{total}[/bold]")
    console.print("\n[nexus.muted]Run without --dry-run to execute.[/nexus.muted]")


class _MCLProcessor:
    """Processes MCL records to rebuild aspect store state.

    Each MCL record is replayed idempotently:
      - UPSERT → put_aspect (overwrites version 0 with MCL payload)
      - DELETE → delete_aspect (soft-deletes all versions)
      - PATH_CHANGED → put_aspect for "path" aspect
    """

    def __init__(self, session: "Session", target: str) -> None:
        from nexus.storage.aspect_service import AspectService

        self._aspect_service = AspectService(session)
        self._target = target
        self._targets = {"search", "versions"} if target == "all" else {target}

    def process(self, row: "OperationLogModel") -> None:
        """Process a single operation_log MCL row.

        Reads entity_urn, aspect_name, change_type, and metadata_snapshot
        from the operation_log row (Key Decision #2: MCL in operation_log).
        """
        change_type = getattr(row, "change_type", "")
        entity_urn = getattr(row, "entity_urn", "")
        aspect_name = getattr(row, "aspect_name", "")
        aspect_value = getattr(row, "metadata_snapshot", None)
        zone_id = getattr(row, "zone_id", None)
        seq = getattr(row, "sequence_number", 0)

        logger.info(
            "Reindex [%s] seq=%d urn=%s aspect=%s change=%s",
            self._target,
            seq,
            entity_urn,
            aspect_name,
            change_type,
        )

        if "search" in self._targets:
            self._rebuild_search(
                change_type=change_type,
                entity_urn=entity_urn,
                aspect_name=aspect_name,
                aspect_value=aspect_value,
                zone_id=zone_id,
            )

        if "versions" in self._targets:
            self._rebuild_versions(
                change_type=change_type,
                entity_urn=entity_urn,
                aspect_name=aspect_name,
                aspect_value=aspect_value,
                zone_id=zone_id,
            )

    def _rebuild_search(
        self,
        *,
        change_type: str,
        entity_urn: str,
        aspect_name: str,
        aspect_value: str | None,
        zone_id: str | None,
    ) -> None:
        """Rebuild search index: apply MCL event to aspect store version 0.

        Uses record_mcl=False to prevent self-amplification: replaying MCL
        rows must not generate new MCL rows into the same table.
        """
        if change_type in ("upsert", "path_changed"):
            if aspect_value is None:
                return
            payload: dict = json.loads(aspect_value)
            self._aspect_service.put_aspect(
                entity_urn,
                aspect_name,
                payload,
                created_by="reindex",
                zone_id=zone_id,
                record_mcl=False,
            )
        elif change_type == "delete":
            self._aspect_service.delete_aspect(
                entity_urn,
                aspect_name,
                zone_id=zone_id,
                record_mcl=False,
            )

    def _rebuild_versions(
        self,
        *,
        change_type: str,
        entity_urn: str,
        aspect_name: str,
        aspect_value: str | None,
        zone_id: str | None,
    ) -> None:
        """Rebuild version history: same as search (put_aspect creates history)."""
        # Version history is built by put_aspect's version-0 swap pattern:
        # each put copies current to version N+1 before overwriting version 0.
        # So replaying upserts in sequence naturally rebuilds version history.
        self._rebuild_search(
            change_type=change_type,
            entity_urn=entity_urn,
            aspect_name=aspect_name,
            aspect_value=aspect_value,
            zone_id=zone_id,
        )

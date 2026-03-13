"""Reindex CLI command — replay MCL to rebuild indices (Issue #2929).

Consumes MetadataChangeLogModel entries via ``MCLRecorder.replay_changes()``
to rebuild aspect store state. Supports incremental and clean modes with
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

    from nexus.storage.models.metadata_change_log import MetadataChangeLogModel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
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
    backend_config: BackendConfig,
) -> None:
    """Replay metadata change log to rebuild indices.

    Uses MCLRecorder.replay_changes() to iterate MCL records and apply
    them to the aspect store. Each MCL record is replayed idempotently:
    upserts overwrite current state, deletes soft-delete aspects.

    Examples:
        nexus reindex --target search
        nexus reindex --target all --dry-run
        nexus reindex --target search --from-sequence 1000
    """
    try:
        nx = get_filesystem(backend_config)

        record_store = getattr(nx, "_record_store", None)
        if record_store is None:
            raise click.ClickException("Reindex requires a local NexusFS with RecordStore")

        from sqlalchemy import func, select

        from nexus.storage.mcl_recorder import MCLRecorder
        from nexus.storage.models.metadata_change_log import MetadataChangeLogModel

        effective_targets = {"search", "versions", "semantic"} if target == "all" else {target}

        with record_store.session_factory() as session:
            # --- Semantic reindex: filesystem walk + CatalogService ---
            if "semantic" in effective_targets:
                _run_semantic_reindex(nx, session, zone, dry_run)

            # --- MCL-based targets (search, versions) ---
            mcl_targets = effective_targets - {"semantic"}
            if mcl_targets:
                mcl_target = "all" if mcl_targets == {"search", "versions"} else mcl_targets.pop()

                # Count total for progress bar
                count_stmt = select(func.count()).select_from(MetadataChangeLogModel)
                if from_sequence is not None:
                    count_stmt = count_stmt.where(
                        MetadataChangeLogModel.sequence_number >= from_sequence
                    )
                if zone is not None:
                    count_stmt = count_stmt.where(MetadataChangeLogModel.zone_id == zone)

                total = session.execute(count_stmt).scalar_one()

                if total == 0:
                    console.print("[yellow]No MCL records to process[/yellow]")
                elif dry_run:
                    _show_dry_run_summary(total, mcl_target)
                else:
                    # Replay MCL via replay_changes() API
                    recorder = MCLRecorder(session)
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

                        for mcl in recorder.replay_changes(
                            from_sequence=from_sequence or 0,
                            zone_id=zone,
                            batch_size=batch_size,
                        ):
                            try:
                                processor.process(mcl)
                                processed += 1
                                last_sequence = mcl.sequence_number
                            except Exception as e:
                                errors += 1
                                console.print(f"[red]Error at seq {mcl.sequence_number}: {e}[/red]")

                            progress.update(task, advance=1)

                    session.commit()

                    # Summary
                    console.print()
                    table = Table(title="Reindex Summary")
                    table.add_column("Metric", style="cyan")
                    table.add_column("Value", style="green")
                    table.add_row("Target", mcl_target)
                    table.add_row("Total MCL records", str(total))
                    table.add_row("Processed", str(processed))
                    table.add_row("Errors", str(errors))
                    table.add_row("Last sequence", str(last_sequence))
                    console.print(table)

                    if errors > 0:
                        console.print(
                            f"\n[yellow]Resume from sequence {last_sequence + 1} to retry:[/yellow]"
                        )
                        console.print(
                            f"  nexus reindex --target {mcl_target} "
                            f"--from-sequence {last_sequence + 1}"
                        )

        nx.close()

    except Exception as e:
        handle_error(e)


def _run_semantic_reindex(
    nx: Any,
    session: "Session",
    zone_id: str | None,
    dry_run: bool,
) -> None:
    """Walk the filesystem and re-extract schemas via CatalogService.

    For each file, computes its URN, reads content, and runs
    CatalogService.extract_schema() to rebuild the schema_metadata aspect.
    """
    import hashlib
    import mimetypes

    from nexus.bricks.catalog.protocol import CatalogService
    from nexus.storage.aspect_service import AspectService

    aspect_service = AspectService(session)
    catalog = CatalogService(aspect_service)

    # Walk the filesystem to discover files
    try:
        all_files = _walk_filesystem(nx, "/")
    except Exception as e:
        console.print(f"[red]Failed to walk filesystem: {e}[/red]")
        return

    if dry_run:
        console.print("\n[bold cyan]Dry Run — semantic reindex[/bold cyan]\n")
        console.print(f"Total files to process: [bold]{len(all_files)}[/bold]")
        console.print("\n[dim]Run without --dry-run to execute.[/dim]")
        return

    processed = 0
    extracted = 0
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Semantic reindex...", total=len(all_files))

        for file_path in all_files:
            try:
                # Compute URN from path
                z = zone_id or "default"
                path_hash = hashlib.sha256(file_path.encode()).hexdigest()[:32]
                urn = f"urn:nexus:file:{z}:{path_hash}"

                # Read file content
                content = nx.read(file_path)
                if isinstance(content, str):
                    content = content.encode()

                # Detect MIME type from filename
                mime_type, _ = mimetypes.guess_type(file_path)

                result = catalog.extract_schema(
                    entity_urn=urn,
                    content=content,
                    mime_type=mime_type,
                    filename=file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path,
                    zone_id=zone_id,
                    created_by="reindex",
                )

                processed += 1
                if result.schema is not None:
                    extracted += 1

            except Exception as e:
                errors += 1
                logger.debug("Semantic reindex failed for %s: %s", file_path, e)

            progress.update(task, advance=1)

    session.commit()

    # Summary
    console.print()
    table = Table(title="Semantic Reindex Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total files", str(len(all_files)))
    table.add_row("Processed", str(processed))
    table.add_row("Schemas extracted", str(extracted))
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
    console.print(f"\n[bold cyan]Dry Run — {target} reindex[/bold cyan]\n")
    console.print(f"Total MCL records to process: [bold]{total}[/bold]")
    console.print("\n[dim]Run without --dry-run to execute.[/dim]")


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

    def process(self, mcl: "MetadataChangeLogModel") -> None:
        """Process a single MCL record."""
        change_type = getattr(mcl, "change_type", "")
        entity_urn = getattr(mcl, "entity_urn", "")
        aspect_name = getattr(mcl, "aspect_name", "")
        aspect_value = getattr(mcl, "aspect_value", None)
        zone_id = getattr(mcl, "zone_id", None)
        seq = getattr(mcl, "sequence_number", 0)

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

"""Workflow Automation commands - manage and execute workflows."""

from __future__ import annotations

import asyncio
import json
import os

import click
from rich.table import Table

from nexus.cli.utils import console, handle_error


def _get_engine_with_storage():  # type: ignore[no-untyped-def]
    """Get or create workflow engine with persistent storage.

    This initializes the workflow engine with database persistence enabled.
    """
    import nexus
    from nexus.workflows import init_engine
    from nexus.workflows.storage import WorkflowStore

    # Get data directory from env or default
    data_dir = os.getenv("NEXUS_DATA_DIR", "./nexus-data")

    # Connect to Nexus to get the metadata store
    nx = nexus.connect(config={"data_dir": str(data_dir)})

    # Get session factory from metadata store
    session_factory = nx.metadata.SessionLocal  # type: ignore[attr-defined]

    # Get tenant_id from Nexus filesystem (or use default)
    tenant_id = getattr(nx, "tenant_id", None) or "default"

    # Create workflow store with tenant_id
    workflow_store = WorkflowStore(session_factory, tenant_id=tenant_id)

    # Initialize engine with storage
    engine = init_engine(
        metadata_store=nx.metadata,  # type: ignore[attr-defined]
        plugin_registry=None,
        workflow_store=workflow_store,
    )

    return engine


def register_commands(cli: click.Group) -> None:
    """Register all workflow commands."""
    cli.add_command(workflows)


@click.group(name="workflows")
def workflows() -> None:
    """Workflow Automation - Manage and execute workflows.

    The Workflow System enables automated pipelines for document processing,
    data transformation, and multi-step operations:
    - File-based workflow definitions (YAML)
    - Event-driven triggers (file writes, deletes, metadata changes)
    - Built-in actions (parse, tag, move, llm, webhook)
    - Plugin-extensible actions and triggers

    Examples:
        nexus workflows load .nexus/workflows/process-invoices.yaml
        nexus workflows list
        nexus workflows test process-invoices --file /inbox/test.pdf
        nexus workflows runs process-invoices
        nexus workflows enable process-invoices
        nexus workflows disable process-invoices
    """
    pass


@workflows.command(name="load")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--enabled/--disabled", default=True, help="Enable workflow after loading")
def workflows_load(file_path: str, enabled: bool) -> None:
    """Load a workflow from a YAML file."""
    try:
        from nexus.workflows import WorkflowLoader

        # Load workflow definition
        definition = WorkflowLoader.load_from_file(file_path)

        # Get workflow engine with persistent storage
        engine = _get_engine_with_storage()

        # Load into engine (will persist to database)
        success = engine.load_workflow(definition, enabled=enabled)

        if success:
            status = "enabled" if enabled else "disabled"
            console.print(
                f"[green]✓[/green] Loaded workflow: [cyan]{definition.name}[/cyan] ({status})"
            )
            console.print(f"  Version: {definition.version}")
            console.print(f"  Triggers: {len(definition.triggers)}")
            console.print(f"  Actions: {len(definition.actions)}")
        else:
            console.print(f"[red]✗[/red] Failed to load workflow from {file_path}")

    except Exception as e:
        handle_error(e)


@workflows.command(name="list")
def workflows_list() -> None:
    """List all loaded workflows."""
    try:
        # Get engine with persistent storage
        engine = _get_engine_with_storage()
        workflow_list = engine.list_workflows()

        if not workflow_list:
            console.print("[yellow]No workflows loaded.[/yellow]")
            console.print("\nLoad workflows with: [cyan]nexus workflows load <file>[/cyan]")
            return

        table = Table(title="Loaded Workflows")
        table.add_column("Name", style="cyan")
        table.add_column("Version", style="green")
        table.add_column("Description")
        table.add_column("Triggers", justify="right")
        table.add_column("Actions", justify="right")
        table.add_column("Status", style="yellow")

        for workflow in workflow_list:
            status = "✓ Enabled" if workflow["enabled"] else "✗ Disabled"
            table.add_row(
                workflow["name"],
                workflow["version"],
                workflow["description"] or "",
                str(workflow["triggers"]),
                str(workflow["actions"]),
                status,
            )

        console.print(table)

    except Exception as e:
        handle_error(e)


@workflows.command(name="test")
@click.argument("workflow_name")
@click.option("--file", "file_path", help="File path to trigger workflow with")
@click.option(
    "--context",
    help="Additional context as JSON",
    default="{}",
)
def workflows_test(workflow_name: str, file_path: str | None, context: str) -> None:
    """Test a workflow execution."""
    try:
        # Parse context
        event_context = json.loads(context)

        # Add file path if provided
        if file_path:
            event_context["file_path"] = file_path

        # Get engine with persistent storage
        engine = _get_engine_with_storage()

        # Execute workflow
        console.print(f"[cyan]Testing workflow:[/cyan] {workflow_name}")
        if file_path:
            console.print(f"[cyan]File:[/cyan] {file_path}")

        execution = asyncio.run(engine.trigger_workflow(workflow_name, event_context))

        if not execution:
            console.print(f"[red]✗[/red] Failed to execute workflow '{workflow_name}'")
            return

        # Display results
        console.print("\n[bold]Execution Results[/bold]")
        console.print(f"Status: {execution.status.value}")
        console.print(f"Actions: {execution.actions_completed}/{execution.actions_total}")

        if execution.started_at and execution.completed_at:
            duration = (execution.completed_at - execution.started_at).total_seconds() * 1000
            console.print(f"Duration: {duration:.2f}ms")

        # Show action results
        if execution.action_results:
            console.print("\n[bold]Action Results:[/bold]")
            for result in execution.action_results:
                status_icon = "✓" if result.success else "✗"
                status_color = "green" if result.success else "red"
                console.print(
                    f"  [{status_color}]{status_icon}[/{status_color}] {result.action_name} ({result.duration_ms:.2f}ms)"
                )
                if result.error:
                    console.print(f"    [red]Error: {result.error}[/red]")

        if execution.error_message:
            console.print(f"\n[red]Error:[/red] {execution.error_message}")

    except Exception as e:
        handle_error(e)


@workflows.command(name="runs")
@click.argument("workflow_name")
@click.option("--limit", default=10, help="Number of executions to show")
def workflows_runs(workflow_name: str, limit: int) -> None:
    """View workflow execution history."""
    try:
        console.print("[yellow]Workflow execution history not yet implemented.[/yellow]")
        console.print(f"This will show the last {limit} executions of '{workflow_name}'")

        # TODO: Implement when database storage is ready
        # from nexus.workflows import get_engine
        # engine = get_engine()
        # executions = engine.get_executions(workflow_name, limit=limit)
        # ... display in table ...

    except Exception as e:
        handle_error(e)


@workflows.command(name="enable")
@click.argument("workflow_name")
def workflows_enable(workflow_name: str) -> None:
    """Enable a workflow."""
    try:
        # Get engine with persistent storage
        engine = _get_engine_with_storage()
        engine.enable_workflow(workflow_name)

        console.print(f"[green]✓[/green] Enabled workflow: [cyan]{workflow_name}[/cyan]")

    except Exception as e:
        handle_error(e)


@workflows.command(name="disable")
@click.argument("workflow_name")
def workflows_disable(workflow_name: str) -> None:
    """Disable a workflow."""
    try:
        # Get engine with persistent storage
        engine = _get_engine_with_storage()
        engine.disable_workflow(workflow_name)

        console.print(f"[yellow]✓[/yellow] Disabled workflow: [cyan]{workflow_name}[/cyan]")

    except Exception as e:
        handle_error(e)


@workflows.command(name="unload")
@click.argument("workflow_name")
def workflows_unload(workflow_name: str) -> None:
    """Unload a workflow."""
    try:
        # Get engine with persistent storage
        engine = _get_engine_with_storage()
        success = engine.unload_workflow(workflow_name)

        if success:
            console.print(f"[green]✓[/green] Unloaded workflow: [cyan]{workflow_name}[/cyan]")
        else:
            console.print(f"[red]✗[/red] Workflow '{workflow_name}' not found")

    except Exception as e:
        handle_error(e)


@workflows.command(name="discover")
@click.argument("directory", type=click.Path(exists=True), default=".nexus/workflows")
@click.option("--load", is_flag=True, help="Load discovered workflows")
def workflows_discover(directory: str, load: bool) -> None:
    """Discover workflows in a directory."""
    try:
        from nexus.workflows import WorkflowLoader

        # Discover workflows
        workflows_found = WorkflowLoader.discover_workflows(directory)

        if not workflows_found:
            console.print(f"[yellow]No workflows found in {directory}[/yellow]")
            return

        console.print(f"[green]✓[/green] Found {len(workflows_found)} workflow(s)")

        # Display discovered workflows
        table = Table(title=f"Workflows in {directory}")
        table.add_column("Name", style="cyan")
        table.add_column("Version", style="green")
        table.add_column("Description")
        table.add_column("Triggers", justify="right")
        table.add_column("Actions", justify="right")

        for workflow in workflows_found:
            table.add_row(
                workflow.name,
                workflow.version,
                workflow.description or "",
                str(len(workflow.triggers)),
                str(len(workflow.actions)),
            )

        console.print(table)

        # Load workflows if requested
        if load:
            # Get engine with persistent storage
            engine = _get_engine_with_storage()
            loaded_count = 0
            for workflow in workflows_found:
                if engine.load_workflow(workflow, enabled=True):
                    loaded_count += 1

            console.print(f"\n[green]✓[/green] Loaded {loaded_count} workflow(s)")

    except Exception as e:
        handle_error(e)

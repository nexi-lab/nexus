"""Workflow Automation commands - manage and execute workflows."""

import asyncio
import json
import os
from typing import Any

import click
from rich.table import Table

from nexus.cli.theme import console
from nexus.cli.utils import handle_error


async def _get_engine_with_storage() -> Any:
    """Get workflow engine from NexusFS (factory-created, no private access).

    Uses the public ``workflow_engine`` attribute already wired by the factory
    via ``_create_workflow_engine()``.  No private attribute access, no concrete
    storage-model imports, no global singleton creation.
    """
    import nexus

    # Get data directory from env or default (consistent with rest of CLI)
    data_dir = os.getenv("NEXUS_DATA_DIR", os.path.join(os.path.expanduser("~"), ".nexus", "data"))

    # Connect to Nexus — factory creates workflow_engine via _create_workflow_engine()
    nx = nexus.connect(config={"data_dir": str(data_dir)})

    engine = getattr(nx, "workflow_engine", None)
    if engine is None:
        raise RuntimeError(
            "Workflow engine not available. Ensure workflows are enabled "
            "and a record store is configured."
        )

    # Load workflows from persistent storage (async startup)
    await engine.startup()

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

    async def _impl() -> None:
        from nexus.bricks.workflows import WorkflowLoader

        # Load workflow definition
        definition = WorkflowLoader.load_from_file(file_path)

        # Get workflow engine with persistent storage
        engine = await _get_engine_with_storage()

        # Load into engine (will persist to database)
        success = engine.load_workflow(definition, enabled=enabled)

        if success:
            status = "enabled" if enabled else "disabled"
            console.print(
                f"[nexus.success]✓[/nexus.success] Loaded workflow: [nexus.value]{definition.name}[/nexus.value] ({status})"
            )
            console.print(f"  Version: {definition.version}")
            console.print(f"  Triggers: {len(definition.triggers)}")
            console.print(f"  Actions: {len(definition.actions)}")
        else:
            console.print(f"[nexus.error]✗[/nexus.error] Failed to load workflow from {file_path}")

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)


@workflows.command(name="list")
def workflows_list() -> None:
    """List all loaded workflows."""

    async def _impl() -> None:
        # Get engine with persistent storage
        engine = await _get_engine_with_storage()
        workflow_list = engine.list_workflows()

        if not workflow_list:
            console.print("[nexus.warning]No workflows loaded.[/nexus.warning]")
            console.print(
                "\nLoad workflows with: [nexus.value]nexus workflows load <file>[/nexus.value]"
            )
            return

        table = Table(title="Loaded Workflows")
        table.add_column("Name", style="nexus.value")
        table.add_column("Version", style="nexus.success")
        table.add_column("Description")
        table.add_column("Triggers", justify="right")
        table.add_column("Actions", justify="right")
        table.add_column("Status", style="nexus.warning")

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

    try:
        asyncio.run(_impl())
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

    async def _impl() -> None:
        # Parse context
        event_context = json.loads(context)

        # Add file path if provided
        if file_path:
            event_context["file_path"] = file_path

        # Get engine with persistent storage
        engine = await _get_engine_with_storage()

        # Execute workflow
        console.print(f"[nexus.value]Testing workflow:[/nexus.value] {workflow_name}")
        if file_path:
            console.print(f"[nexus.path]File:[/nexus.path] {file_path}")

        execution = await engine.trigger_workflow(workflow_name, event_context)

        if not execution:
            console.print(
                f"[nexus.error]✗[/nexus.error] Failed to execute workflow '{workflow_name}'"
            )
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
                status_color = "nexus.success" if result.success else "nexus.error"
                console.print(
                    f"  [{status_color}]{status_icon}[/{status_color}] {result.action_name} ({result.duration_ms:.2f}ms)"
                )
                if result.error:
                    console.print(f"    [nexus.error]Error: {result.error}[/nexus.error]")

        if execution.error_message:
            console.print(f"\n[nexus.error]Error:[/nexus.error] {execution.error_message}")

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)


@workflows.command(name="runs")
@click.argument("workflow_name")
@click.option("--limit", default=10, help="Number of executions to show")
def workflows_runs(workflow_name: str, limit: int) -> None:
    """View workflow execution history."""
    try:
        console.print(
            "[nexus.warning]Workflow execution history not yet implemented.[/nexus.warning]"
        )
        console.print(f"This will show the last {limit} executions of '{workflow_name}'")
        # TODO(#1443): implement workflow execution history when database storage is ready

    except Exception as e:
        handle_error(e)


@workflows.command(name="enable")
@click.argument("workflow_name")
def workflows_enable(workflow_name: str) -> None:
    """Enable a workflow."""

    async def _impl() -> None:
        # Get engine with persistent storage
        engine = await _get_engine_with_storage()
        engine.enable_workflow(workflow_name)

        console.print(
            f"[nexus.success]✓[/nexus.success] Enabled workflow: [nexus.value]{workflow_name}[/nexus.value]"
        )

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)


@workflows.command(name="disable")
@click.argument("workflow_name")
def workflows_disable(workflow_name: str) -> None:
    """Disable a workflow."""

    async def _impl() -> None:
        # Get engine with persistent storage
        engine = await _get_engine_with_storage()
        engine.disable_workflow(workflow_name)

        console.print(
            f"[nexus.warning]✓[/nexus.warning] Disabled workflow: [nexus.value]{workflow_name}[/nexus.value]"
        )

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)


@workflows.command(name="unload")
@click.argument("workflow_name")
def workflows_unload(workflow_name: str) -> None:
    """Unload a workflow."""

    async def _impl() -> None:
        # Get engine with persistent storage
        engine = await _get_engine_with_storage()
        success = engine.unload_workflow(workflow_name)

        if success:
            console.print(
                f"[nexus.success]✓[/nexus.success] Unloaded workflow: [nexus.value]{workflow_name}[/nexus.value]"
            )
        else:
            console.print(f"[nexus.error]✗[/nexus.error] Workflow '{workflow_name}' not found")

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)


@workflows.command(name="discover")
@click.argument("directory", type=click.Path(exists=True), default=".nexus/workflows")
@click.option("--load", is_flag=True, help="Load discovered workflows")
def workflows_discover(directory: str, load: bool) -> None:
    """Discover workflows in a directory."""

    async def _impl() -> None:
        from nexus.bricks.workflows import WorkflowLoader

        # Discover workflows
        workflows_found = WorkflowLoader.discover_workflows(directory)

        if not workflows_found:
            console.print(f"[nexus.warning]No workflows found in {directory}[/nexus.warning]")
            return

        console.print(f"[nexus.success]✓[/nexus.success] Found {len(workflows_found)} workflow(s)")

        # Display discovered workflows
        table = Table(title=f"Workflows in {directory}")
        table.add_column("Name", style="nexus.value")
        table.add_column("Version", style="nexus.success")
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
            engine = await _get_engine_with_storage()
            loaded_count = 0
            for workflow in workflows_found:
                if engine.load_workflow(workflow, enabled=True):
                    loaded_count += 1

            console.print(f"\n[nexus.success]✓[/nexus.success] Loaded {loaded_count} workflow(s)")

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)

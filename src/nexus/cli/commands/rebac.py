"""ReBAC (Relationship-Based Access Control) CLI commands.

Manage authorization relationships using Zanzibar-style ReBAC.
Enables team-based permissions, hierarchical access, and dynamic inheritance.
"""

from __future__ import annotations

import sys

import click
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    get_tenant_id,
    handle_error,
)


@click.group(name="rebac")
def rebac() -> None:
    """Relationship-Based Access Control (ReBAC) commands.

    Manage authorization relationships using Zanzibar-style ReBAC.
    Enables team-based permissions, hierarchical access, and dynamic inheritance.

    Examples:
        nexus rebac create agent alice member-of group eng-team
        nexus rebac check agent alice read file file123
        nexus rebac expand read file file123
        nexus rebac delete <tuple-id>
    """
    pass


@rebac.command(name="create")
@click.argument("subject_type", type=str)
@click.argument("subject_id", type=str)
@click.argument("relation", type=str)
@click.argument("object_type", type=str)
@click.argument("object_id", type=str)
@click.option("--expires", type=str, default=None, help="Expiration time (ISO format)")
@click.option(
    "--tenant-id",
    type=str,
    default=None,
    help="Tenant ID for multi-tenant isolation (can also use NEXUS_TENANT_ID env var)",
)
@click.option(
    "--subject-relation",
    type=str,
    default=None,
    help="Subject relation for userset-as-subject (e.g., 'member' for group#member)",
)
@click.option(
    "--wildcard",
    is_flag=True,
    help="Use wildcard subject (*:*) for public access (overrides subject_type/subject_id)",
)
@click.option(
    "--column-config",
    type=str,
    default=None,
    help='JSON column config for dynamic_viewer (e.g., \'{"mode":"whitelist","visible_columns":["name","email"]}\')',
)
@add_backend_options
def rebac_create(
    subject_type: str,
    subject_id: str,
    relation: str,
    object_type: str,
    object_id: str,
    expires: str | None,
    tenant_id: str | None,
    subject_relation: str | None,
    wildcard: bool,
    column_config: str | None,
    backend_config: BackendConfig,
) -> None:
    """Create a relationship tuple.

    Creates a (subject, relation, object) tuple representing a relationship.
    Supports multi-tenant isolation via --tenant-id or NEXUS_TENANT_ID env var.

    Advanced Features:
        --subject-relation: Grant to entire groups (userset-as-subject)
        --wildcard: Grant public access to anyone
        --column-config: Column-level permissions for dynamic_viewer

    Examples:
        # Basic: Alice is member of eng-team
        nexus rebac create agent alice member-of group eng-team

        # Multi-tenant: Eng-team owns file123
        nexus rebac create group eng-team owner-of file file123 --tenant-id org_acme

        # Userset-as-subject: Grant all eng-team members editor access
        nexus rebac create group eng-team editor-of file readme.txt --subject-relation member

        # Wildcard: Public read access to readme
        nexus rebac create '*' '*' viewer-of file readme.txt --wildcard

        # Temporary access (expires in 1 hour)
        nexus rebac create agent bob viewer-of file secret --expires 2025-12-31T23:59:59

        # Dynamic viewer with column-level permissions (CSV only)
        nexus rebac create agent alice dynamic_viewer file /data/users.csv --column-config '{"hidden_columns":["password"],"aggregations":{"age":"mean"},"visible_columns":["name","email"]}'
    """
    try:
        nx = get_filesystem(backend_config)

        # Parse expiration time if provided
        expires_at = None
        if expires:
            from datetime import datetime

            try:
                expires_at = datetime.fromisoformat(expires)
            except ValueError:
                console.print(f"[red]Error:[/red] Invalid date format: {expires}")
                console.print("Use ISO format: 2025-12-31T23:59:59")
                nx.close()
                sys.exit(1)

        # Get tenant_id from parameter or environment
        tenant = get_tenant_id(tenant_id)

        # Parse column_config JSON if provided
        column_config_dict = None
        if column_config:
            import json

            try:
                column_config_dict = json.loads(column_config)
            except json.JSONDecodeError as e:
                console.print(f"[red]Error:[/red] Invalid JSON in column-config: {e}")
                nx.close()
                sys.exit(1)

        # Build subject tuple
        subject_tuple: tuple[str, str] | tuple[str, str, str]
        if wildcard:
            # Wildcard subject for public access
            subject_tuple = ("*", "*")
            subject_display = "*:*"
        elif subject_relation:
            # Userset-as-subject (3-tuple)
            subject_tuple = (subject_type, subject_id, subject_relation)
            subject_display = f"{subject_type}:{subject_id}#{subject_relation}"
        else:
            # Regular subject (2-tuple)
            subject_tuple = (subject_type, subject_id)
            subject_display = f"{subject_type}:{subject_id}"

        # Create tuple
        tuple_id = nx.rebac_create(  # type: ignore[attr-defined]
            subject=subject_tuple,
            relation=relation,
            object=(object_type, object_id),
            expires_at=expires_at,
            tenant_id=tenant,
            column_config=column_config_dict,
        )

        nx.close()

        console.print("[green]✓[/green] Created relationship tuple")
        console.print(f"  Tuple ID: [cyan]{tuple_id}[/cyan]")
        console.print(f"  Subject: [yellow]{subject_display}[/yellow]")
        if wildcard:
            console.print("    [dim](wildcard - public access)[/dim]")
        elif subject_relation:
            console.print(
                f"    [dim](userset-as-subject: all '{subject_relation}' of {subject_type}:{subject_id})[/dim]"
            )
        console.print(f"  Relation: [magenta]{relation}[/magenta]")
        console.print(f"  Object: [yellow]{object_type}:{object_id}[/yellow]")
        if tenant:
            console.print(f"  Tenant: [blue]{tenant}[/blue]")
        if expires_at:
            console.print(f"  Expires: [dim]{expires_at.isoformat()}[/dim]")
        if column_config_dict:
            console.print("  Column Config:")
            if column_config_dict.get("hidden_columns"):
                console.print(
                    f"    Hidden Columns: [red]{', '.join(column_config_dict['hidden_columns'])}[/red]"
                )
            if column_config_dict.get("visible_columns"):
                console.print(
                    f"    Visible Columns: [green]{', '.join(column_config_dict['visible_columns'])}[/green]"
                )
            if column_config_dict.get("aggregations"):
                console.print("    Aggregations:")
                for col, op in column_config_dict["aggregations"].items():
                    console.print(f"      {col}: [yellow]{op}[/yellow]")

    except Exception as e:
        handle_error(e)


@rebac.command(name="list")
@click.option("--subject-type", type=str, help="Filter by subject type")
@click.option("--subject-id", type=str, help="Filter by subject ID")
@click.option("--object-type", type=str, help="Filter by object type")
@click.option("--object-id", type=str, help="Filter by object ID")
@click.option("--relation", type=str, help="Filter by relation")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "compact"]),
    default="table",
    help="Output format",
)
@click.option("--limit", type=int, help="Limit number of results")
@add_backend_options
def rebac_list_cmd(
    subject_type: str | None,
    subject_id: str | None,
    object_type: str | None,
    object_id: str | None,
    relation: str | None,
    output_format: str,
    limit: int | None,
    backend_config: BackendConfig,
) -> None:
    """List relationship tuples with optional filters.

    Examples:
        # List all tuples
        nexus rebac list

        # List tuples for a specific file
        nexus rebac list --object-type file --object-id /workspace/test.txt

        # List all tuples for user alice
        nexus rebac list --subject-type user --subject-id alice

        # List all editor relations
        nexus rebac list --relation direct_editor

        # Compact format
        nexus rebac list --format compact

        # JSON output
        nexus rebac list --format json
    """
    try:
        import json

        nx = get_filesystem(backend_config)

        # Build filters
        subject = None
        if subject_type and subject_id:
            subject = (subject_type, subject_id)

        obj = None
        if object_type and object_id:
            obj = (object_type, object_id)

        # List tuples
        tuples = nx.rebac_list_tuples(  # type: ignore[attr-defined]
            subject=subject,
            object=obj,
            relation=relation,
        )

        nx.close()

        # Apply limit if specified
        if limit and limit > 0:
            tuples = tuples[:limit]

        # Display results
        if not tuples:
            console.print("[yellow]No tuples found[/yellow]")
            return

        if output_format == "json":
            console.print(json.dumps(tuples, indent=2, default=str))
        elif output_format == "compact":
            for t in tuples:
                subj = f"{t['subject_type']}:{t['subject_id']}"
                if t.get("subject_relation"):
                    subj += f"#{t['subject_relation']}"
                obj_str = f"{t['object_type']}:{t['object_id']}"
                console.print(f"{subj} → {t['relation']} → {obj_str}")
        else:
            # Table format
            table = Table(title=f"ReBAC Tuples ({len(tuples)} found)")
            table.add_column("Tuple ID", style="dim", no_wrap=True)
            table.add_column("Subject", style="yellow")
            table.add_column("Relation", style="magenta")
            table.add_column("Object", style="cyan")
            table.add_column("Tenant", style="blue")

            for t in tuples:
                # Format subject
                subj = f"{t['subject_type']}:{t['subject_id']}"
                if t.get("subject_relation"):
                    subj += f"#{t['subject_relation']}"

                # Format object
                obj_str = f"{t['object_type']}:{t['object_id']}"

                # Truncate IDs for display
                tuple_id = t["tuple_id"]
                if len(tuple_id) > 36:
                    tuple_id = tuple_id[:8] + "..." + tuple_id[-8:]

                table.add_row(
                    tuple_id,
                    subj,
                    t["relation"],
                    obj_str,
                    t.get("tenant_id") or "-",
                )

            console.print(table)

    except Exception as e:
        handle_error(e)


@rebac.command(name="delete")
@click.argument("tuple_id", type=str)
@add_backend_options
def rebac_delete_cmd(
    tuple_id: str,
    backend_config: BackendConfig,
) -> None:
    """Delete a relationship tuple.

    Examples:
        nexus rebac delete 550e8400-e29b-41d4-a716-446655440000
    """
    try:
        nx = get_filesystem(backend_config)

        # Delete tuple
        deleted = nx.rebac_delete(tuple_id)  # type: ignore[attr-defined]

        nx.close()

        if deleted:
            console.print(f"[green]✓[/green] Deleted relationship tuple [cyan]{tuple_id}[/cyan]")
        else:
            console.print(f"[yellow]Tuple not found:[/yellow] {tuple_id}")

    except Exception as e:
        handle_error(e)


@rebac.command(name="check")
@click.argument("subject_type", type=str)
@click.argument("subject_id", type=str)
@click.argument("permission", type=str)
@click.argument("object_type", type=str)
@click.argument("object_id", type=str)
@add_backend_options
def rebac_check_cmd(
    subject_type: str,
    subject_id: str,
    permission: str,
    object_type: str,
    object_id: str,
    backend_config: BackendConfig,
) -> None:
    """Check if subject has permission on object.

    Uses graph traversal and caching to determine if permission is granted.

    Examples:
        # Does alice have read permission on file123?
        nexus rebac check agent alice read file file123

        # Does bob have write permission on workspace?
        nexus rebac check agent bob write workspace main

        # Does eng-team have owner permission on project?
        nexus rebac check group eng-team owner file project-folder
    """
    try:
        nx = get_filesystem(backend_config)

        # Check permission
        granted = nx.rebac_check(  # type: ignore[attr-defined]
            subject=(subject_type, subject_id),
            permission=permission,
            object=(object_type, object_id),
        )

        nx.close()

        # Display result
        if granted:
            console.print("[green]✓ GRANTED[/green]")
            console.print(
                f"  [yellow]{subject_type}:{subject_id}[/yellow] has [magenta]{permission}[/magenta] on [yellow]{object_type}:{object_id}[/yellow]"
            )
        else:
            console.print("[red]✗ DENIED[/red]")
            console.print(
                f"  [yellow]{subject_type}:{subject_id}[/yellow] does NOT have [magenta]{permission}[/magenta] on [yellow]{object_type}:{object_id}[/yellow]"
            )

    except Exception as e:
        handle_error(e)


@rebac.command(name="expand")
@click.argument("permission", type=str)
@click.argument("object_type", type=str)
@click.argument("object_id", type=str)
@add_backend_options
def rebac_expand_cmd(
    permission: str,
    object_type: str,
    object_id: str,
    backend_config: BackendConfig,
) -> None:
    """Find all subjects with a given permission on an object.

    Uses recursive graph traversal to find all subjects.

    Examples:
        # Who has read permission on file123?
        nexus rebac expand read file file123

        # Who has write permission on workspace?
        nexus rebac expand write workspace main

        # Who owns the project folder?
        nexus rebac expand owner file project-folder
    """
    try:
        nx = get_filesystem(backend_config)

        # Expand permission
        subjects = nx.rebac_expand(  # type: ignore[attr-defined]
            permission=permission,
            object=(object_type, object_id),
        )

        nx.close()

        # Display results
        if not subjects:
            console.print(
                f"[yellow]No subjects found with[/yellow] [magenta]{permission}[/magenta] [yellow]on[/yellow] [cyan]{object_type}:{object_id}[/cyan]"
            )
            return

        console.print(
            f"[green]Found {len(subjects)} subjects[/green] with [magenta]{permission}[/magenta] on [cyan]{object_type}:{object_id}[/cyan]"
        )
        console.print()

        table = Table(title=f"Subjects with '{permission}' permission")
        table.add_column("Subject Type", style="yellow")
        table.add_column("Subject ID", style="cyan")

        for subj_type, subj_id in sorted(subjects):
            table.add_row(subj_type, subj_id)

        console.print(table)

    except Exception as e:
        handle_error(e)


@rebac.command(name="explain")
@click.argument("subject_type", type=str)
@click.argument("subject_id", type=str)
@click.argument("permission", type=str)
@click.argument("object_type", type=str)
@click.argument("object_id", type=str)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed path information")
@add_backend_options
def rebac_explain_cmd(
    subject_type: str,
    subject_id: str,
    permission: str,
    object_type: str,
    object_id: str,
    verbose: bool,
    backend_config: BackendConfig,
) -> None:
    """Explain why a subject has or doesn't have permission on an object.

    This command traces through the permission graph to show exactly why
    a permission check succeeded or failed. Useful for debugging access issues.

    Examples:
        # Why does alice have read permission on file123?
        nexus rebac explain agent alice read file file123

        # Why doesn't bob have write permission?
        nexus rebac explain agent bob write workspace main

        # Show detailed path information
        nexus rebac explain agent alice read file file123 --verbose
    """
    try:
        import json

        nx = get_filesystem(backend_config)

        # Get explanation
        explanation = nx.rebac_explain(  # type: ignore[attr-defined]
            subject=(subject_type, subject_id),
            permission=permission,
            object=(object_type, object_id),
        )

        nx.close()

        # Display result header with metadata
        result = explanation["result"]
        cached = explanation["cached"]
        metadata = explanation.get("metadata", {})

        # Header
        if result:
            console.print("[green]✓ GRANTED[/green]")
        else:
            console.print("[red]✗ DENIED[/red]")

        # Metadata
        console.print()
        console.print(f"[dim]Subject:[/dim]      [yellow]{subject_type}:{subject_id}[/yellow]")
        console.print(f"[dim]Permission:[/dim]   [magenta]{permission}[/magenta]")
        console.print(f"[dim]Resource:[/dim]     [cyan]{object_type}:{object_id}[/cyan]")
        if metadata:
            console.print(f"[dim]Timestamp:[/dim]   {metadata.get('timestamp', 'N/A')}")
            console.print(f"[dim]Request ID:[/dim]  {metadata.get('request_id', 'N/A')}")

        # Display schema rule if available
        console.print()
        successful_path = explanation.get("successful_path")
        if successful_path and "expanded_to" in successful_path:
            expanded = successful_path["expanded_to"]
            console.print(
                f"[bold]Schema Rule:[/bold] {permission}([cyan]{object_type}[/cyan]) = {' ∪ '.join(expanded)}"
            )

        # Display detailed explanation
        console.print()
        if cached:
            console.print("[bold]Reason:[/bold] Result from cache")
        elif successful_path:
            console.print("[bold]Proof:[/bold]")
            _display_proof_tree(successful_path, depth=0, step_number=[1])
            console.print()
            console.print("[bold]Conclusion:[/bold]")
            via = successful_path.get("via_userset") or successful_path.get("via_union_member")
            if via:
                console.print(
                    f"  {via}([cyan]{object_type}:{object_id}[/cyan]) = [green]true[/green] ⇒ {permission}(...) = [green]true[/green] ⇒ [green]GRANTED[/green]"
                )
            else:
                console.print(
                    f"  {permission}([cyan]{object_type}:{object_id}[/cyan]) = [green]true[/green] ⇒ [green]GRANTED[/green]"
                )
        elif not result:
            console.print("[bold]Reason:[/bold] No valid permission path found")
            paths = explanation.get("paths", [])
            if paths and verbose:
                console.print()
                console.print("[bold]Attempted paths:[/bold]")
                for i, path in enumerate(paths, 1):
                    console.print(
                        f"  {i}. {path.get('permission')} on {path.get('object')} - [red]NOT FOUND[/red]"
                    )
        else:
            console.print("[bold]Reason:[/bold] Permission granted (no path information)")

        # Display verbose info if requested
        if verbose:
            console.print()
            console.print("[bold]Full Path Details (JSON):[/bold]")
            console.print(json.dumps(explanation, indent=2, default=str))

    except Exception as e:
        handle_error(e)


def _format_tuple(tuple_info: dict) -> str:
    """Format a tuple for display.

    Args:
        tuple_info: Tuple dictionary with subject, relation, object

    Returns:
        Formatted tuple string
    """
    subj_type = tuple_info.get("subject_type", "?")
    subj_id = tuple_info.get("subject_id", "?")
    subj_rel = tuple_info.get("subject_relation")
    relation = tuple_info.get("relation", "?")
    obj_type = tuple_info.get("object_type", "?")
    obj_id = tuple_info.get("object_id", "?")

    if subj_rel:
        subject = f"({subj_type}, {subj_id}, {relation}, {subj_type}:{subj_id}#{subj_rel})"
    else:
        subject = f"{subj_type}:{subj_id}"

    return f"({subject}, {relation}, {obj_type}:{obj_id})"


def _display_proof_tree(path: dict, depth: int = 0, step_number: list[int] | None = None) -> None:
    """Display a proof tree for permission explanation.

    Args:
        path: Path dictionary from explain API
        depth: Current indentation depth
        step_number: Mutable list containing current step number
    """
    if step_number is None:
        step_number = [1]

    indent = "  " * depth
    permission = path.get("permission", "?")
    obj = path.get("object", "?")
    granted = path.get("granted", False)

    # Main check
    step = step_number[0]
    step_number[0] += 1
    console.print(f"{indent}{step}) Check [magenta]{permission}[/magenta]([cyan]{obj}[/cyan])")

    # Show expansion
    if "expanded_to" in path:
        relations = path["expanded_to"]
        console.print(f"{indent}   → Expand to {{[magenta]{', '.join(relations)}[/magenta]}}")

    # Show union/intersection
    if "union" in path:
        union_rels = path["union"]
        console.print(f"{indent}   Schema: {permission} = {' ∪ '.join(union_rels)}")

    # Check direct tuple
    if path.get("direct_relation") and "tuple" in path:
        tuple_info = path["tuple"]
        tuple_id = tuple_info.get("tuple_id", "N/A")
        tuple_str = _format_tuple(tuple_info)
        console.print(f"{indent}   → [green]FOUND[/green] tuple: {tuple_str}")
        console.print(f"{indent}   → Tuple ID: [dim]{tuple_id}[/dim]")
    elif path.get("direct_relation"):
        console.print(f"{indent}   → [green]FOUND[/green] direct relation")

    # Show parent traversal
    if "tupleToUserset" in path:
        ttu = path["tupleToUserset"]
        tupleset = ttu.get("tupleset", "?")
        computed = ttu.get("computedUserset", "?")
        console.print(f"{indent}   Rule: {permission}(f) = ∃ p. {tupleset}(f) = p ∧ {computed}(p)")

        found_parents = ttu.get("found_parents", [])
        if found_parents:
            for parent in found_parents:
                if isinstance(parent, (list, tuple)) and len(parent) >= 2:
                    parent_type, parent_id = parent[0], parent[1]
                    console.print(
                        f"{indent}   • {tupleset}([cyan]{obj}[/cyan]) = [cyan]{parent_type}:{parent_id}[/cyan]  [green]✓[/green]"
                    )
                else:
                    console.print(
                        f"{indent}   • {tupleset}([cyan]{obj}[/cyan]) = [cyan]{parent}[/cyan]  [green]✓[/green]"
                    )
        else:
            console.print(f"{indent}   • No parent found → [red]SKIPPED[/red]")

    # Show status
    if "error" in path:
        console.print(f"{indent}   → [red]ERROR:[/red] {path['error']}")
    elif not granted and not path.get("sub_paths"):
        console.print(f"{indent}   → [red]NOT FOUND[/red]")

    # Display sub-paths recursively
    if "sub_paths" in path:
        for sub_path in path["sub_paths"]:
            if sub_path.get("granted"):
                console.print()
                _display_proof_tree(sub_path, depth + 1, step_number)
                break  # Only show successful path


@rebac.command(name="check-batch")
@click.argument("checks_file", type=click.Path(exists=True))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "summary"]),
    default="table",
    help="Output format",
)
@add_backend_options
def rebac_check_batch_cmd(
    checks_file: str,
    output_format: str,
    backend_config: BackendConfig,
) -> None:
    """Batch permission checks from a JSON file.

    Efficiently check multiple permissions at once. Much faster than individual checks.

    The JSON file should contain an array of check objects:
    [
        {"subject": ["agent", "alice"], "permission": "read", "object": ["file", "f1"]},
        {"subject": ["agent", "bob"], "permission": "write", "object": ["file", "f2"]}
    ]

    Examples:
        # Create checks.json with multiple checks
        echo '[
            {"subject": ["agent", "alice"], "permission": "read", "object": ["file", "f1"]},
            {"subject": ["agent", "alice"], "permission": "write", "object": ["file", "f1"]},
            {"subject": ["agent", "bob"], "permission": "read", "object": ["file", "f2"]}
        ]' > checks.json

        # Run batch check
        nexus rebac check-batch checks.json

        # JSON output
        nexus rebac check-batch checks.json --format json

        # Summary only
        nexus rebac check-batch checks.json --format summary
    """
    try:
        import json

        nx = get_filesystem(backend_config)

        # Load checks from file
        with open(checks_file) as f:
            checks_data = json.load(f)

        if not isinstance(checks_data, list):
            console.print("[red]Error:[/red] Checks file must contain a JSON array")
            nx.close()
            sys.exit(1)

        # Convert to batch check format
        checks = []
        for i, check in enumerate(checks_data):
            try:
                subject = tuple(check["subject"])
                permission = check["permission"]
                obj = tuple(check["object"])
                checks.append((subject, permission, obj))
            except (KeyError, TypeError) as e:
                console.print(f"[red]Error:[/red] Invalid check at index {i}: {e}")
                nx.close()
                sys.exit(1)

        # Perform batch check with Rust acceleration
        console.print(
            f"[cyan]Checking {len(checks)} permissions (Rust acceleration enabled)...[/cyan]"
        )
        results = nx.rebac_manager.rebac_check_batch_fast(checks)  # type: ignore[attr-defined]
        nx.close()

        # Output results
        if output_format == "json":
            # JSON output
            output = []
            for check, result in zip(checks_data, results, strict=False):
                output.append(
                    {
                        **check,
                        "result": result,
                    }
                )
            console.print(json.dumps(output, indent=2))

        elif output_format == "summary":
            # Summary only
            allowed_count = sum(results)
            denied_count = len(results) - allowed_count
            console.print(f"[green]✓ {allowed_count} allowed[/green]")
            console.print(f"[red]✗ {denied_count} denied[/red]")
            console.print(f"  Total: {len(results)}")

        else:
            # Table output (default)
            table = Table(title=f"Batch Check Results ({len(checks)} checks)")
            table.add_column("#", style="dim")
            table.add_column("Subject", style="yellow")
            table.add_column("Permission", style="magenta")
            table.add_column("Object", style="cyan")
            table.add_column("Result", style="bold")

            for i, ((subject, permission, obj), result) in enumerate(
                zip(checks, results, strict=False)
            ):
                result_text = "[green]✓ ALLOWED[/green]" if result else "[red]✗ DENIED[/red]"
                table.add_row(
                    str(i + 1),
                    f"{subject[0]}:{subject[1]}",
                    permission,
                    f"{obj[0]}:{obj[1]}",
                    result_text,
                )

            console.print(table)

            # Summary
            allowed_count = sum(results)
            denied_count = len(results) - allowed_count
            console.print()
            console.print(
                f"Summary: [green]{allowed_count} allowed[/green], [red]{denied_count} denied[/red]"
            )

    except Exception as e:
        handle_error(e)


@rebac.command(name="namespace-create")
@click.argument("object_type", type=str)
@click.option(
    "--config-file", type=click.Path(exists=True), help="JSON/YAML file with namespace config"
)
@click.option(
    "--relations",
    type=str,
    multiple=True,
    help="Add relation (format: name or name:union:rel1,rel2)",
)
@click.option(
    "--permission",
    type=str,
    multiple=True,
    help="Add permission mapping (format: perm:rel1,rel2,rel3)",
)
@add_backend_options
def namespace_create(
    object_type: str,
    config_file: str | None,
    relations: tuple[str, ...],
    permission: tuple[str, ...],
    backend_config: BackendConfig,
) -> None:
    """Create or update a namespace configuration.

    Define custom object types with their relations and permissions.

    Examples:
        # From config file (JSON/YAML)
        nexus rebac namespace-create document --config-file document.json

        # Inline definition
        nexus rebac namespace-create project \\
          --relations owner --relations editor --relations viewer:union:editor,owner \\
          --permission read:viewer,editor,owner --permission write:editor,owner

        # Config file format (JSON):
        {
          "relations": {
            "owner": {},
            "editor": {},
            "viewer": {"union": ["editor", "owner"]}
          },
          "permissions": {
            "read": ["viewer", "editor", "owner"],
            "write": ["editor", "owner"]
          }
        }
    """
    try:
        import json

        import yaml

        nx = get_filesystem(backend_config)

        config = {}

        if config_file:
            # Load from file
            with open(config_file) as f:
                if config_file.endswith((".yaml", ".yml")):
                    config = yaml.safe_load(f)
                else:
                    config = json.load(f)
        else:
            # Build from CLI options
            config["relations"] = {}
            config["permissions"] = {}

            # Parse relations
            for rel in relations:
                if ":" in rel:
                    parts = rel.split(":")
                    name = parts[0]
                    op = parts[1]
                    values = parts[2].split(",") if len(parts) > 2 else []
                    config["relations"][name] = {op: values}
                else:
                    config["relations"][rel] = {}

            # Parse permissions
            for perm in permission:
                perm_name, rels = perm.split(":", 1)
                config["permissions"][perm_name] = rels.split(",")

        # Create namespace
        nx.namespace_create(object_type=object_type, config=config)  # type: ignore[attr-defined]

        console.print(f"[green]✓[/green] Created namespace for '{object_type}'")

    except Exception as e:
        handle_error(e)


@rebac.command(name="namespace-list")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
@add_backend_options
def namespace_list(
    output_format: str,
    backend_config: BackendConfig,
) -> None:
    """List all registered namespace configurations.

    Examples:
        # List as table
        nexus rebac namespace-list

        # JSON output
        nexus rebac namespace-list --format json
    """
    try:
        nx = get_filesystem(backend_config)

        namespaces = nx.namespace_list()  # type: ignore[attr-defined]

        if output_format == "json":
            import json

            console.print(json.dumps(namespaces, indent=2))
        else:
            if not namespaces:
                console.print("[yellow]No namespaces registered[/yellow]")
                return

            table = Table(title="ReBAC Namespaces")
            table.add_column("Object Type", style="cyan")
            table.add_column("Relations", style="green")
            table.add_column("Permissions", style="magenta")
            table.add_column("Created", style="dim")

            for ns in namespaces:
                relations = ", ".join(ns["config"]["relations"].keys())
                permissions = ", ".join(ns["config"]["permissions"].keys())
                created = (
                    ns["created_at"][:19]
                    if isinstance(ns["created_at"], str)
                    else str(ns["created_at"])[:19]
                )

                table.add_row(
                    ns["object_type"],
                    relations,
                    permissions,
                    created,
                )

            console.print(table)

    except Exception as e:
        handle_error(e)


@rebac.command(name="namespace-get")
@click.argument("object_type", type=str)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["yaml", "json"]),
    default="yaml",
    help="Output format",
)
@add_backend_options
def namespace_get(
    object_type: str,
    output_format: str,
    backend_config: BackendConfig,
) -> None:
    """Get namespace configuration for an object type.

    Examples:
        # View file namespace
        nexus rebac namespace-get file

        # JSON output
        nexus rebac namespace-get group --format json
    """
    try:
        import json

        import yaml

        nx = get_filesystem(backend_config)

        ns = nx.get_namespace(object_type)  # type: ignore[attr-defined]

        if ns is None:
            console.print(f"[red]✗[/red] Namespace '{object_type}' not found")
            sys.exit(1)

        if output_format == "json":
            console.print(json.dumps(ns, indent=2))
        else:
            console.print(yaml.dump(ns, default_flow_style=False))

    except Exception as e:
        handle_error(e)


@rebac.command(name="namespace-delete")
@click.argument("object_type", type=str)
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@add_backend_options
def namespace_delete(
    object_type: str,
    yes: bool,
    backend_config: BackendConfig,
) -> None:
    """Delete a namespace configuration.

    WARNING: This does not delete existing tuples for this object type.

    Examples:
        # Delete with confirmation
        nexus rebac namespace-delete document

        # Skip confirmation
        nexus rebac namespace-delete document --yes
    """
    try:
        if not yes:
            confirm = input(f"Delete namespace '{object_type}'? (y/N): ")
            if confirm.lower() != "y":
                console.print("[yellow]Cancelled[/yellow]")
                return

        nx = get_filesystem(backend_config)

        deleted = nx.namespace_delete(object_type)  # type: ignore[attr-defined]

        if deleted:
            console.print(f"[green]✓[/green] Deleted namespace '{object_type}'")
        else:
            console.print(f"[red]✗[/red] Namespace '{object_type}' not found")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register ReBAC commands with the CLI.

    Args:
        cli: The Click CLI group to register commands with
    """
    cli.add_command(rebac)

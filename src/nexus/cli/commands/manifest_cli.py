"""Access manifest CLI commands — agent tool access management.

Maps to /api/v2/access-manifests/* endpoints via ManifestClient.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.manifest import ManifestClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def manifest() -> None:
    """Agent access manifest management.

    \b
    Create, inspect, and evaluate agent access manifests that control
    which tools and data sources an agent can use.

    \b
    Examples:
        nexus manifest create agent_alice --name "dev tools" --entry "read_*:allow"
        nexus manifest list --json
        nexus manifest evaluate <id> --tool-name read_file
    """


@manifest.command("create")
@click.argument("agent_id")
@click.option("--name", required=True, help="Human-readable manifest name")
@click.option(
    "--entry",
    "entries",
    multiple=True,
    required=True,
    help="Tool entry as 'pattern:permission' (e.g. 'read_*:allow'). Repeatable.",
)
@click.option("--zone-id", default="root", show_default=True, help="Zone ID")
@click.option("--valid-hours", default=720, show_default=True, help="Validity in hours")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ManifestClient)
def manifest_create(
    client: ManifestClient,
    agent_id: str,
    name: str,
    entries: tuple[str, ...],
    zone_id: str,
    valid_hours: int,
) -> ServiceResult:
    """Create an access manifest for an agent.

    \b
    Examples:
        nexus manifest create agent_alice --name "dev" --entry "read_*:allow"
        nexus manifest create agent_alice --name "ops" --entry "write_file:allow" --entry "delete_*:deny"
    """
    parsed_entries = []
    for entry_str in entries:
        parts = entry_str.rsplit(":", 1)
        if len(parts) != 2:
            raise click.BadParameter(
                f"Entry must be 'pattern:permission', got '{entry_str}'",
                param_hint="--entry",
            )
        parsed_entries.append({"tool_pattern": parts[0], "permission": parts[1]})

    data = client.create(
        agent_id, name=name, entries=parsed_entries, zone_id=zone_id, valid_hours=valid_hours
    )

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print("[nexus.success]Manifest created[/nexus.success]")
        console.print(f"  Manifest ID: {d.get('manifest_id', d.get('id', 'N/A'))}")
        console.print(f"  Agent:       {d.get('agent_id', agent_id)}")
        console.print(f"  Name:        {d.get('name', name)}")
        manifest_entries = d.get("entries", [])
        if manifest_entries:
            console.print("  Entries:")
            for e in manifest_entries:
                console.print(f"    - {e.get('tool_pattern', '?')}: {e.get('permission', '?')}")

    return ServiceResult(data=data, human_formatter=_render)


@manifest.command("list")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ManifestClient)
def manifest_list(client: ManifestClient) -> ServiceResult:
    """List access manifests.

    \b
    Examples:
        nexus manifest list
        nexus manifest list --json
    """
    data = client.list()

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        manifests = d.get("manifests", [])
        if not manifests:
            console.print("[nexus.warning]No manifests[/nexus.warning]")
            return

        table = Table(title=f"Access Manifests ({len(manifests)})")
        table.add_column("ID", style="nexus.muted")
        table.add_column("Agent")
        table.add_column("Name")
        table.add_column("Entries")
        table.add_column("Valid Until", style="nexus.muted")

        for m in manifests:
            entry_count = len(m.get("entries", []))
            table.add_row(
                str(m.get("manifest_id", m.get("id", "")))[:12],
                m.get("agent_id", ""),
                m.get("name", ""),
                str(entry_count),
                m.get("valid_until", "")[:19],
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@manifest.command("show")
@click.argument("manifest_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ManifestClient)
def manifest_show(client: ManifestClient, manifest_id: str) -> ServiceResult:
    """Show manifest details.

    \b
    Examples:
        nexus manifest show mfst_123 --json
    """
    data = client.show(manifest_id)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print(f"[bold cyan]Manifest: {manifest_id}[/bold cyan]")
        console.print(f"  Name:       {d.get('name', 'N/A')}")
        console.print(f"  Agent:      {d.get('agent_id', 'N/A')}")
        console.print(f"  Valid From: {d.get('valid_from', 'N/A')[:19]}")
        console.print(f"  Valid Until:{d.get('valid_until', 'N/A')[:19]}")
        manifest_entries = d.get("entries", [])
        if manifest_entries:
            console.print("  Entries:")
            for e in manifest_entries:
                console.print(f"    - {e.get('tool_pattern', '?')}: {e.get('permission', '?')}")

    return ServiceResult(data=data, human_formatter=_render)


@manifest.command("evaluate")
@click.argument("manifest_id")
@click.option("--tool-name", required=True, help="Tool name to evaluate access for")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ManifestClient)
def manifest_evaluate(client: ManifestClient, manifest_id: str, tool_name: str) -> ServiceResult:
    """Test tool access against a manifest.

    \b
    Examples:
        nexus manifest evaluate mfst_123 --tool-name read_file
        nexus manifest evaluate mfst_123 --tool-name write_file --json
    """
    data = client.evaluate(manifest_id, tool_name=tool_name)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        permission = d.get("permission", "deny")
        allowed = permission == "allow"
        status = (
            "[nexus.success]Allowed[/nexus.success]"
            if allowed
            else "[nexus.error]Denied[/nexus.error]"
        )
        console.print(f"Tool '{tool_name}': {status}")
        if d.get("manifest_id"):
            console.print(f"  Manifest: {d['manifest_id']}")

    return ServiceResult(data=data, human_formatter=_render)


@manifest.command("revoke")
@click.argument("manifest_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ManifestClient)
def manifest_revoke(client: ManifestClient, manifest_id: str) -> ServiceResult:
    """Revoke an access manifest.

    \b
    Examples:
        nexus manifest revoke mfst_123
    """
    data = client.revoke(manifest_id)
    return ServiceResult(data=data, message=f"Manifest {manifest_id} revoked")

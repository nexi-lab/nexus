"""Identity CLI commands — agent DID, credentials, and verification.

Issue #2812. Migrated from httpx to gRPC rpc_call (Issue #3318).
"""

from __future__ import annotations

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console, rpc_call


@click.group()
def identity() -> None:
    """Agent identity and credential management.

    \b
    Manage agent DIDs, public keys, and verifiable credentials.

    \b
    Examples:
        nexus identity show agent_alice --json
        nexus identity verify agent_alice --message <b64> --signature <b64>
    """


@identity.command("show")
@click.argument("agent_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def identity_show(
    agent_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show agent identity (DID, public key, capabilities)."""
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "identity_show", agent_id=agent_id)

        def _render(d: dict) -> None:
            console.print(f"[bold cyan]Identity: {agent_id}[/bold cyan]")
            console.print(f"  DID:        {d.get('did', 'N/A')}")
            console.print(f"  Key ID:     {d.get('key_id', 'N/A')}")
            console.print(f"  Public Key: {d.get('public_key_hex', d.get('public_key', 'N/A'))}")
            console.print(f"  Algorithm:  {d.get('algorithm', 'Ed25519')}")

        render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")


@identity.command("verify")
@click.argument("agent_id")
@click.option("--message", required=True, help="Base64-encoded message to verify")
@click.option("--signature", required=True, help="Base64-encoded signature")
@click.option("--key-id", default=None, help="Key ID (uses newest active key if omitted)")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def identity_verify(
    agent_id: str,
    message: str,
    signature: str,
    key_id: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Verify an agent's signature."""
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "identity_verify",
                agent_id=agent_id,
                message=message,
                signature=signature,
                key_id=key_id,
            )

        def _render(d: dict) -> None:
            valid = d.get("valid", False)
            status = "[green]Valid[/green]" if valid else "[red]Invalid[/red]"
            console.print(f"[bold cyan]Verification: {agent_id}[/bold cyan]")
            console.print(f"  Status: {status}")
            if d.get("reason"):
                console.print(f"  Reason: {d['reason']}")

        render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")

"""Identity CLI commands — agent DID, credentials, and verification.

Maps to /api/v2/agents/*/identity and /api/v2/credentials/* endpoints.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.identity import IdentityClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def identity() -> None:
    """Agent identity and credential management.

    \b
    Manage agent DIDs, public keys, and verifiable credentials.

    \b
    Examples:
        nexus identity show agent_alice --json
        nexus identity verify agent_alice --message <b64> --signature <b64>
        nexus identity credentials agent_alice
    """


@identity.command("show")
@click.argument("agent_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=IdentityClient)
def identity_show(client: IdentityClient, agent_id: str) -> ServiceResult:
    """Show agent identity (DID, public key, capabilities).

    \b
    Examples:
        nexus identity show agent_alice
        nexus identity show agent_alice --json
    """
    data = client.show(agent_id)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print(f"[bold nexus.value]Identity: {agent_id}[/bold nexus.value]")
        console.print(f"  DID:        {d.get('did', 'N/A')}")
        console.print(f"  Key ID:     {d.get('key_id', 'N/A')}")
        console.print(f"  Public Key: {d.get('public_key_hex', d.get('public_key', 'N/A'))}")
        console.print(f"  Algorithm:  {d.get('algorithm', 'Ed25519')}")

    return ServiceResult(data=data, human_formatter=_render)


@identity.command("verify")
@click.argument("agent_id")
@click.option("--message", required=True, help="Base64-encoded message to verify")
@click.option("--signature", required=True, help="Base64-encoded signature")
@click.option("--key-id", default=None, help="Key ID (uses newest active key if omitted)")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=IdentityClient)
def identity_verify(
    client: IdentityClient,
    agent_id: str,
    message: str,
    signature: str,
    key_id: str | None,
) -> ServiceResult:
    """Verify an agent's signature.

    \b
    Examples:
        nexus identity verify agent_alice --message <b64msg> --signature <b64sig>
        nexus identity verify agent_alice --message <b64> --signature <b64> --key-id key_1 --json
    """
    data = client.verify(agent_id, message=message, signature=signature, key_id=key_id)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        valid = d.get("valid", False)
        status = (
            "[nexus.success]Valid[/nexus.success]"
            if valid
            else "[nexus.error]Invalid[/nexus.error]"
        )
        console.print(f"[bold nexus.value]Verification: {agent_id}[/bold nexus.value]")
        console.print(f"  Status: {status}")
        if d.get("reason"):
            console.print(f"  Reason: {d['reason']}")

    return ServiceResult(data=data, human_formatter=_render)


@identity.command("credentials")
@click.argument("agent_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=IdentityClient)
def identity_credentials(client: IdentityClient, agent_id: str) -> ServiceResult:
    """List agent's active credentials.

    \b
    Examples:
        nexus identity credentials agent_alice
        nexus identity credentials agent_alice --json
    """
    data = client.credentials_list(agent_id)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        creds = d.get("credentials", [])
        if not creds:
            console.print("[nexus.warning]No active credentials[/nexus.warning]")
            return

        table = Table(title=f"Credentials for {agent_id}")
        table.add_column("ID", style="nexus.muted")
        table.add_column("Issuer DID")
        table.add_column("Expires", style="nexus.muted")
        table.add_column("Active")

        for c in creds:
            table.add_row(
                c.get("credential_id", "")[:12],
                c.get("issuer_did", "")[:20],
                c.get("expires_at", "")[:19],
                "Yes" if c.get("is_active") else "No",
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@identity.command("passport")
@click.argument("agent_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=IdentityClient)
def identity_passport(client: IdentityClient, agent_id: str) -> ServiceResult:
    """Show Digital Agent Passport (identity + credentials combined).

    \b
    Examples:
        nexus identity passport agent_alice --json
    """
    # Passport combines identity and credentials in a single view
    identity_data = client.show(agent_id)
    creds_data = client.credentials_list(agent_id)
    data = {**identity_data, "credentials": creds_data.get("credentials", [])}

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print(f"[bold nexus.value]Agent Passport: {agent_id}[/bold nexus.value]")
        console.print(f"  DID:        {d.get('did', 'N/A')}")
        console.print(f"  Public Key: {d.get('public_key_hex', d.get('public_key', 'N/A'))}")
        creds = d.get("credentials", [])
        console.print(f"  Credentials: {len(creds)} active")
        for c in creds[:5]:
            console.print(
                f"    - {c.get('credential_id', '')[:12]}: {c.get('issuer_did', '')[:20]}"
            )

    return ServiceResult(data=data, human_formatter=_render)

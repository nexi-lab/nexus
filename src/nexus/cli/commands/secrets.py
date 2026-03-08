"""User secrets management CLI commands.

Provides commands for managing encrypted user secrets:
- nexus secrets set NAME VALUE
- nexus secrets get NAME
- nexus secrets list
- nexus secrets delete NAME
"""

import sys
from typing import TYPE_CHECKING

import click
from rich.table import Table

from nexus.cli.utils import console, handle_error

if TYPE_CHECKING:
    from nexus.bricks.auth.secrets.service import UserSecretsService


@click.group()
def secrets() -> None:
    """Manage user secrets (encrypted key-value store).

    Store API keys, tokens, and other sensitive values that can be
    referenced in plugin/agent configs using nexus-secret:NAME.

    \b
    Examples:
        nexus secrets set OPENAI_API_KEY sk-...
        nexus secrets get OPENAI_API_KEY
        nexus secrets list
        nexus secrets delete OPENAI_API_KEY
    """


def _get_service(db_path: str | None = None) -> "UserSecretsService":
    """Build a UserSecretsService from local database."""
    import os
    from pathlib import Path

    # Import model so its table is registered on Base.metadata
    import nexus.storage.models.auth  # noqa: F401
    from nexus.bricks.auth.secrets.crypto import SecretsCrypto
    from nexus.bricks.auth.secrets.service import UserSecretsService
    from nexus.storage.models._base import Base
    from nexus.storage.record_store import SQLAlchemyRecordStore

    db = db_path or os.environ.get("NEXUS_DB_PATH")
    if not db:
        default_db = Path.home() / ".nexus" / "nexus.db"
        if default_db.exists():
            db = str(default_db)
        else:
            console.print(
                "[red]Error:[/red] No database found. Set NEXUS_DB_PATH or use --db-path."
            )
            sys.exit(1)

    db_url = f"sqlite:///{db}" if not db.startswith("sqlite") else db
    record_store = SQLAlchemyRecordStore(db_url=db_url)
    Base.metadata.create_all(record_store.engine, checkfirst=True)

    crypto = SecretsCrypto(record_store=record_store)
    return UserSecretsService(record_store=record_store, crypto=crypto)


def _get_user_id() -> str:
    """Get current user ID from environment or default."""
    import os

    return os.environ.get("NEXUS_USER_ID", os.environ.get("USER", "default"))


@secrets.command("set")
@click.argument("name")
@click.argument("value")
@click.option("--db-path", type=str, default=None, help="Path to database")
@click.option("--zone-id", type=str, default=None, help="Zone ID (default: root)")
def set_secret(name: str, value: str, db_path: str | None, zone_id: str | None) -> None:
    """Set a secret value (creates or updates)."""
    try:
        service = _get_service(db_path)
        user_id = _get_user_id()

        kwargs: dict = {
            "user_id": user_id,
            "name": name,
            "value": value,
        }
        if zone_id:
            kwargs["zone_id"] = zone_id

        secret_id = service.set_secret(**kwargs)
        console.print(f"[green]Secret {name!r} saved[/green] (id={secret_id})")
    except Exception as e:
        handle_error(e)


@secrets.command("get")
@click.argument("name")
@click.option("--db-path", type=str, default=None, help="Path to database")
@click.option("--zone-id", type=str, default=None, help="Zone ID (default: root)")
def get_secret(name: str, db_path: str | None, zone_id: str | None) -> None:
    """Get a secret value (prints to stdout)."""
    try:
        service = _get_service(db_path)
        user_id = _get_user_id()

        kwargs: dict = {
            "user_id": user_id,
            "name": name,
        }
        if zone_id:
            kwargs["zone_id"] = zone_id

        value = service.get_secret_value(**kwargs)
        if value is None:
            console.print(f"[yellow]Secret {name!r} not found[/yellow]")
            sys.exit(1)
        else:
            # Print raw value (no formatting) for piping
            click.echo(value)
    except Exception as e:
        handle_error(e)


@secrets.command("list")
@click.option("--db-path", type=str, default=None, help="Path to database")
@click.option("--zone-id", type=str, default=None, help="Zone ID (default: root)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def list_secrets(db_path: str | None, zone_id: str | None, json_output: bool) -> None:
    """List all secret names (values are never shown)."""
    try:
        service = _get_service(db_path)
        user_id = _get_user_id()

        kwargs: dict = {"user_id": user_id}
        if zone_id:
            kwargs["zone_id"] = zone_id

        secrets_list = service.list_secrets(**kwargs)

        if not secrets_list:
            console.print("[yellow]No secrets found[/yellow]")
            return

        if json_output:
            import json

            click.echo(json.dumps(secrets_list, indent=2))
        else:
            table = Table(title="User Secrets")
            table.add_column("Name", style="cyan")
            table.add_column("Created", style="dim")
            table.add_column("Updated", style="dim")

            for s in secrets_list:
                table.add_row(s["name"], s.get("created_at", ""), s.get("updated_at", ""))

            console.print(table)
    except Exception as e:
        handle_error(e)


@secrets.command("delete")
@click.argument("name")
@click.option("--db-path", type=str, default=None, help="Path to database")
@click.option("--zone-id", type=str, default=None, help="Zone ID (default: root)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def delete_secret(name: str, db_path: str | None, zone_id: str | None, yes: bool) -> None:
    """Delete a secret by name."""
    try:
        if not yes and not click.confirm(f"Delete secret {name!r}?"):
            return

        service = _get_service(db_path)
        user_id = _get_user_id()

        kwargs: dict = {
            "user_id": user_id,
            "name": name,
        }
        if zone_id:
            kwargs["zone_id"] = zone_id

        deleted = service.delete_secret(**kwargs)
        if deleted:
            console.print(f"[green]Secret {name!r} deleted[/green]")
        else:
            console.print(f"[yellow]Secret {name!r} not found[/yellow]")
            sys.exit(1)
    except Exception as e:
        handle_error(e)

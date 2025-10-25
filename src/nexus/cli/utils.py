"""CLI utilities - Common helpers for Nexus CLI commands."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console

import nexus
from nexus import NexusFilesystem
from nexus.core.exceptions import NexusError, NexusFileNotFoundError, ValidationError

console = Console()

# Global options
BACKEND_OPTION = click.option(
    "--backend",
    type=click.Choice(["local", "gcs"]),
    default="local",
    help="Backend type: local (default) or gcs (Google Cloud Storage)",
    show_default=True,
)

DATA_DIR_OPTION = click.option(
    "--data-dir",
    type=click.Path(),
    default=lambda: os.getenv("NEXUS_DATA_DIR", "./nexus-data"),
    help="Path to Nexus data directory (for local backend and metadata DB). Can also be set via NEXUS_DATA_DIR environment variable.",
    show_default=True,
)

GCS_BUCKET_OPTION = click.option(
    "--gcs-bucket",
    type=str,
    default=None,
    help="GCS bucket name (required when backend=gcs)",
)

GCS_PROJECT_OPTION = click.option(
    "--gcs-project",
    type=str,
    default=None,
    help="GCP project ID (optional for GCS backend)",
)

GCS_CREDENTIALS_OPTION = click.option(
    "--gcs-credentials",
    type=click.Path(exists=True),
    default=None,
    help="Path to GCS service account credentials JSON file",
)

CONFIG_OPTION = click.option(
    "--config",
    type=click.Path(exists=True),
    default=None,
    help="Path to Nexus config file (nexus.yaml)",
)

DB_URL_OPTION = click.option(
    "--db-url",
    type=str,
    default=None,
    help="Database URL for metadata store (e.g., postgresql://user:pass@host/db). If not provided, uses NEXUS_DATABASE_URL env var or SQLite.",
)


class BackendConfig:
    """Configuration for backend connection."""

    def __init__(
        self,
        backend: str = "local",
        data_dir: str = "./nexus-data",
        config_path: str | None = None,
        gcs_bucket: str | None = None,
        gcs_project: str | None = None,
        gcs_credentials: str | None = None,
        db_url: str | None = None,
    ):
        self.backend = backend
        self.data_dir = data_dir
        self.config_path = config_path
        self.gcs_bucket = gcs_bucket
        self.gcs_project = gcs_project
        self.gcs_credentials = gcs_credentials
        self.db_url = db_url


def add_backend_options(func: Any) -> Any:
    """Decorator to add all backend-related options to a command and pass them via context."""
    import functools

    @CONFIG_OPTION
    @BACKEND_OPTION
    @DATA_DIR_OPTION
    @GCS_BUCKET_OPTION
    @GCS_PROJECT_OPTION
    @GCS_CREDENTIALS_OPTION
    @DB_URL_OPTION
    @functools.wraps(func)
    def wrapper(
        config: str | None,
        backend: str,
        data_dir: str,
        gcs_bucket: str | None,
        gcs_project: str | None,
        gcs_credentials: str | None,
        db_url: str | None,
        **kwargs: Any,
    ) -> Any:
        # Create backend config and pass to function
        backend_config = BackendConfig(
            backend=backend,
            data_dir=data_dir,
            config_path=config,
            gcs_bucket=gcs_bucket,
            gcs_project=gcs_project,
            gcs_credentials=gcs_credentials,
            db_url=db_url,
        )
        return func(backend_config=backend_config, **kwargs)

    return wrapper


def get_filesystem(
    backend_config: BackendConfig, enforce_permissions: bool = True
) -> NexusFilesystem:
    """Get Nexus filesystem instance from backend configuration.

    Args:
        backend_config: Backend configuration
        enforce_permissions: Whether to enforce permissions (default: True)

    Returns:
        NexusFilesystem instance
    """
    try:
        if backend_config.config_path:
            # Use explicit config file
            return nexus.connect(config=backend_config.config_path)
        elif backend_config.backend == "gcs":
            # Use GCS backend via nexus.connect()
            if not backend_config.gcs_bucket:
                console.print("[red]Error:[/red] --gcs-bucket is required when using --backend=gcs")
                sys.exit(1)

            # Determine database configuration (priority order):
            # 1. --db-url CLI option
            # 2. NEXUS_DATABASE_URL environment variable
            # 3. POSTGRES_URL environment variable
            # 4. Default to SQLite file
            db_url = (
                backend_config.db_url
                or os.getenv("NEXUS_DATABASE_URL")
                or os.getenv("POSTGRES_URL")
            )

            config = {
                "backend": "gcs",
                "gcs_bucket_name": backend_config.gcs_bucket,
                "gcs_project_id": backend_config.gcs_project,
                "gcs_credentials_path": backend_config.gcs_credentials,
                "enforce_permissions": enforce_permissions,
            }

            # Add database configuration
            if db_url:
                # Using PostgreSQL (or other database via URL)
                config["db_url"] = db_url
            else:
                # Default to SQLite
                config["db_path"] = str(Path(backend_config.data_dir) / "nexus-gcs-metadata.db")

            return nexus.connect(config=config)
        else:
            # Use local backend (default)
            return nexus.connect(
                config={
                    "data_dir": backend_config.data_dir,
                    "enforce_permissions": enforce_permissions,
                }
            )
    except Exception as e:
        console.print(f"[red]Error connecting to Nexus:[/red] {e}")
        sys.exit(1)


def get_default_filesystem() -> NexusFilesystem:
    """Get Nexus filesystem instance with default configuration.

    Used by commands that don't accept backend options (e.g., memory commands).
    Uses NEXUS_DATA_DIR env var or default data directory.

    Returns:
        NexusFilesystem instance
    """
    try:
        import os

        data_dir = os.environ.get("NEXUS_DATA_DIR", str(Path.home() / ".nexus"))
        return nexus.connect(config={"data_dir": data_dir})
    except Exception as e:
        console.print(f"[red]Error connecting to Nexus:[/red] {e}")
        sys.exit(1)


def handle_error(e: Exception) -> None:
    """Handle errors with beautiful output."""
    if isinstance(e, PermissionError):
        console.print(f"[red]Permission Denied:[/red] {e}")
    elif isinstance(e, NexusFileNotFoundError):
        # Don't add "File not found:" prefix - the exception message already contains it
        console.print(f"[red]Error:[/red] {e}")
    elif isinstance(e, ValidationError):
        console.print(f"[red]Validation Error:[/red] {e}")
    elif isinstance(e, NexusError):
        console.print(f"[red]Nexus Error:[/red] {e}")
    else:
        console.print(f"[red]Unexpected error:[/red] {e}")
    sys.exit(1)

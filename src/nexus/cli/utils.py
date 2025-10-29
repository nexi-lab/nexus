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

REMOTE_URL_OPTION = click.option(
    "--remote-url",
    type=str,
    default=None,
    envvar="NEXUS_URL",
    help="Remote Nexus server URL (e.g., http://localhost:8080). Can also use NEXUS_URL env var.",
)

REMOTE_API_KEY_OPTION = click.option(
    "--remote-api-key",
    type=str,
    default=None,
    envvar="NEXUS_API_KEY",
    help="API key for remote authentication. Can also use NEXUS_API_KEY env var.",
)

CONFIG_OPTION = click.option(
    "--config",
    type=click.Path(exists=True),
    default=None,
    help="Path to Nexus config file (nexus.yaml)",
)

# P0 Enhanced Context Options
SUBJECT_OPTION = click.option(
    "--subject",
    type=str,
    default=None,
    help="Subject for operation in format 'type:id' (e.g., 'user:alice', 'agent:bot1'). Can also be set via NEXUS_SUBJECT env var.",
)

TENANT_ID_OPTION = click.option(
    "--tenant-id",
    type=str,
    default=None,
    help="Tenant ID for multi-tenant isolation (e.g., 'org_acme'). Can also be set via NEXUS_TENANT_ID env var.",
)

IS_ADMIN_OPTION = click.option(
    "--is-admin",
    is_flag=True,
    default=False,
    help="Run operation with admin privileges (requires admin capabilities).",
)

IS_SYSTEM_OPTION = click.option(
    "--is-system",
    is_flag=True,
    default=False,
    help="Run operation as system (limited to /system/* paths).",
)

ADMIN_CAPABILITIES_OPTION = click.option(
    "--admin-capability",
    "admin_capabilities",
    multiple=True,
    type=str,
    help="Admin capability to grant (can be specified multiple times). Example: admin:read:*",
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
        remote_url: str | None = None,
        remote_api_key: str | None = None,
    ):
        self.backend = backend
        self.data_dir = data_dir
        self.config_path = config_path
        self.gcs_bucket = gcs_bucket
        self.gcs_project = gcs_project
        self.gcs_credentials = gcs_credentials
        self.remote_url = remote_url
        self.remote_api_key = remote_api_key


def add_backend_options(func: Any) -> Any:
    """Decorator to add all backend-related options to a command and pass them via context."""
    import functools

    @CONFIG_OPTION
    @BACKEND_OPTION
    @DATA_DIR_OPTION
    @GCS_BUCKET_OPTION
    @GCS_PROJECT_OPTION
    @GCS_CREDENTIALS_OPTION
    @REMOTE_URL_OPTION
    @REMOTE_API_KEY_OPTION
    @functools.wraps(func)
    def wrapper(
        config: str | None,
        backend: str,
        data_dir: str,
        gcs_bucket: str | None,
        gcs_project: str | None,
        gcs_credentials: str | None,
        remote_url: str | None,
        remote_api_key: str | None,
        **kwargs: Any,
    ) -> Any:
        # Create backend config and pass to function
        backend_config = BackendConfig(
            backend=backend,
            data_dir=data_dir,
            config_path=config,
            remote_url=remote_url,
            remote_api_key=remote_api_key,
            gcs_bucket=gcs_bucket,
            gcs_project=gcs_project,
            gcs_credentials=gcs_credentials,
        )
        return func(backend_config=backend_config, **kwargs)

    return wrapper


def get_filesystem(
    backend_config: BackendConfig, enforce_permissions: bool | None = None
) -> NexusFilesystem:
    """Get Nexus filesystem instance from backend configuration.

    Args:
        backend_config: Backend configuration
        enforce_permissions: Whether to enforce permissions (None = use environment/config default)

    Returns:
        NexusFilesystem instance
    """
    try:
        if backend_config.remote_url:
            # Use remote server connection
            from nexus.remote import RemoteNexusFS

            return RemoteNexusFS(
                server_url=backend_config.remote_url,
                api_key=backend_config.remote_api_key,
            )
        elif backend_config.config_path:
            # Use explicit config file (will load environment variables via load_config)
            return nexus.connect(config=backend_config.config_path)
        elif backend_config.backend == "gcs":
            # Use GCS backend via nexus.connect()
            if not backend_config.gcs_bucket:
                console.print("[red]Error:[/red] --gcs-bucket is required when using --backend=gcs")
                sys.exit(1)
            config: dict[str, Any] = {
                "backend": "gcs",
                "gcs_bucket_name": backend_config.gcs_bucket,
                "gcs_project_id": backend_config.gcs_project,
                "gcs_credentials_path": backend_config.gcs_credentials,
                "db_path": str(Path(backend_config.data_dir) / "nexus-gcs-metadata.db"),
            }
            # Only set enforce_permissions if explicitly provided
            if enforce_permissions is not None:
                config["enforce_permissions"] = enforce_permissions
            return nexus.connect(config=config)
        else:
            # Use local backend (default)
            # Let nexus.connect() load from environment variables if not explicitly set
            config = {"data_dir": backend_config.data_dir}
            if enforce_permissions is not None:
                config["enforce_permissions"] = enforce_permissions
            return nexus.connect(config=config)
    except Exception as e:
        console.print(f"[red]Error connecting to Nexus:[/red] {e}")
        sys.exit(1)


def get_default_filesystem() -> NexusFilesystem:
    """Get Nexus filesystem instance with default configuration.

    Used by commands that don't accept backend options (e.g., memory commands).
    Supports both local and remote modes via environment variables:
    - NEXUS_URL: Remote server URL (if set, uses remote mode)
    - NEXUS_API_KEY: API key for remote authentication
    - NEXUS_DATA_DIR: Data directory for local mode (default: ~/.nexus)

    Returns:
        NexusFilesystem instance (RemoteNexusFS if NEXUS_URL is set, otherwise local)
    """
    try:
        import os

        # Check for remote URL first (priority over local)
        remote_url = os.environ.get("NEXUS_URL")
        if remote_url:
            # Use remote server connection
            from nexus.remote import RemoteNexusFS

            return RemoteNexusFS(
                server_url=remote_url,
                api_key=os.environ.get("NEXUS_API_KEY"),
            )

        # Fall back to local mode
        data_dir = os.environ.get("NEXUS_DATA_DIR", str(Path.home() / ".nexus"))
        return nexus.connect(config={"data_dir": data_dir})
    except Exception as e:
        console.print(f"[red]Error connecting to Nexus:[/red] {e}")
        sys.exit(1)


def get_subject_from_env() -> tuple[str, str] | None:
    """Get subject from environment variables.

    Checks NEXUS_SUBJECT_TYPE and NEXUS_SUBJECT_ID environment variables.

    Returns:
        Subject tuple (type, id) or None if not set

    Example:
        export NEXUS_SUBJECT_TYPE=user
        export NEXUS_SUBJECT_ID=alice
    """
    subject_type = os.getenv("NEXUS_SUBJECT_TYPE")
    subject_id = os.getenv("NEXUS_SUBJECT_ID")

    if subject_type and subject_id:
        return (subject_type, subject_id)

    return None


def parse_subject(subject_str: str | None) -> tuple[str, str] | None:
    """Parse subject string in format 'type:id'.

    Args:
        subject_str: Subject string like 'user:alice' or 'agent:bot1'

    Returns:
        Tuple of (type, id) or None if not provided

    Example:
        >>> parse_subject("user:alice")
        ("user", "alice")
        >>> parse_subject("agent:bot1")
        ("agent", "bot1")
    """
    if not subject_str:
        # Try environment variable
        env_subject = os.getenv("NEXUS_SUBJECT")
        if env_subject:
            subject_str = env_subject
        else:
            return get_subject_from_env()

    if not subject_str:
        return None

    if ":" not in subject_str:
        console.print(f"[red]Error:[/red] Invalid subject format: {subject_str}")
        console.print(
            "[yellow]Expected format:[/yellow] type:id (e.g., 'user:alice', 'agent:bot1')"
        )
        sys.exit(1)

    parts = subject_str.split(":", 1)
    return (parts[0], parts[1])


def get_tenant_id(tenant_id: str | None) -> str | None:
    """Get tenant ID from parameter or environment.

    Args:
        tenant_id: Tenant ID from CLI parameter

    Returns:
        Tenant ID or None
    """
    if tenant_id:
        return tenant_id
    return os.getenv("NEXUS_TENANT_ID")


def create_operation_context(
    subject: str | None = None,
    tenant_id: str | None = None,
    is_admin: bool = False,
    is_system: bool = False,
    admin_capabilities: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Create operation context from CLI parameters.

    This creates a context dict that can be passed to NexusFS operations.
    The dict format is compatible with both old OperationContext and new
    EnhancedOperationContext.

    Args:
        subject: Subject string in format 'type:id'
        tenant_id: Tenant ID
        is_admin: Whether operation has admin privileges
        is_system: Whether operation is system-level
        admin_capabilities: Admin capabilities

    Returns:
        Context dictionary
    """
    # Parse subject
    subject_tuple = parse_subject(subject)

    # Get tenant_id
    tenant_id = get_tenant_id(tenant_id)

    # Build context
    context: dict[str, Any] = {}

    if subject_tuple:
        context["subject"] = subject_tuple

    if tenant_id:
        context["tenant"] = tenant_id  # NexusFS methods use 'tenant' not 'tenant_id'

    if is_admin:
        context["is_admin"] = True

    if is_system:
        context["is_system"] = True

    if admin_capabilities:
        context["admin_capabilities"] = set(admin_capabilities)

    return context


def add_context_options(func: Any) -> Any:
    """Decorator to add enhanced context options to a command.

    Adds --subject, --tenant-id, --is-admin, --is-system, --admin-capability
    options to commands.
    """
    import functools

    @ADMIN_CAPABILITIES_OPTION
    @IS_SYSTEM_OPTION
    @IS_ADMIN_OPTION
    @TENANT_ID_OPTION
    @SUBJECT_OPTION
    @functools.wraps(func)
    def wrapper(
        subject: str | None,
        tenant_id: str | None,
        is_admin: bool,
        is_system: bool,
        admin_capabilities: tuple[str, ...],
        **kwargs: Any,
    ) -> Any:
        # Create context and pass to function
        context = create_operation_context(
            subject=subject,
            tenant_id=tenant_id,
            is_admin=is_admin,
            is_system=is_system,
            admin_capabilities=admin_capabilities,
        )
        return func(operation_context=context, **kwargs)

    return wrapper


def get_subject(subject_option: str | None) -> tuple[str, str] | None:
    """Get subject from CLI option or environment variables.

    Precedence:
    1. CLI option (--subject type:id)
    2. Environment variables (NEXUS_SUBJECT_TYPE + NEXUS_SUBJECT_ID)

    Args:
        subject_option: Subject from CLI --subject option

    Returns:
        Subject tuple (type, id) or None

    Example:
        # From CLI: nexus read /file.txt --subject user:alice
        # From env: NEXUS_SUBJECT_TYPE=user NEXUS_SUBJECT_ID=alice nexus read /file.txt
    """
    # CLI option takes precedence
    if subject_option:
        return parse_subject(subject_option)

    # Fall back to environment variables
    return get_subject_from_env()


def handle_error(e: Exception) -> None:
    """Handle errors with beautiful output and proper exit codes.

    Exit codes follow Unix conventions:
    - 0: Success (not used here)
    - 1: General error
    - 2: File/resource not found
    - 3: Permission denied
    """
    # Import exception types here to avoid circular imports
    from nexus.core.exceptions import NexusPermissionError
    from nexus.core.router import AccessDeniedError

    if isinstance(e, (PermissionError, AccessDeniedError, NexusPermissionError)):
        console.print(f"[red]Permission Denied:[/red] {e}")
        sys.exit(3)  # Exit code 3 for permission errors
    elif isinstance(e, NexusFileNotFoundError):
        # Don't add "File not found:" prefix - the exception message already contains it
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)  # Exit code 2 for not found errors
    elif isinstance(e, ValidationError):
        console.print(f"[red]Validation Error:[/red] {e}")
        sys.exit(1)
    elif isinstance(e, NexusError):
        console.print(f"[red]Nexus Error:[/red] {e}")
        sys.exit(1)
    else:
        console.print(f"[red]Unexpected error:[/red] {e}")
        sys.exit(1)

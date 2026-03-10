"""CLI utilities - Common helpers for Nexus CLI commands."""

from __future__ import annotations

import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import click
from rich.console import Console

import nexus
from nexus import NexusFilesystem
from nexus.cli.exit_codes import ExitCode
from nexus.contracts.exceptions import NexusError, NexusFileNotFoundError, ValidationError

if TYPE_CHECKING:
    pass

console = Console()

# Global options
REMOTE_URL_OPTION = click.option(
    "--remote-url",
    type=str,
    default=None,
    envvar="NEXUS_URL",
    help="Remote Nexus server URL (e.g., http://localhost:2026). Can also use NEXUS_URL env var.",
)

REMOTE_API_KEY_OPTION = click.option(
    "--remote-api-key",
    type=str,
    default=None,
    envvar="NEXUS_API_KEY",
    help="API key for remote authentication. Can also use NEXUS_API_KEY env var.",
)

# P0 Enhanced Context Options
SUBJECT_OPTION = click.option(
    "--subject",
    type=str,
    default=None,
    help="Subject for operation in format 'type:id' (e.g., 'user:alice', 'agent:bot1'). Can also be set via NEXUS_SUBJECT env var.",
)

ZONE_ID_OPTION = click.option(
    "--zone-id",
    type=str,
    default=None,
    envvar="NEXUS_ZONE_ID",
    help="Zone ID for multi-zone isolation (e.g., 'org_acme').",
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


def add_backend_options(func: Any) -> Any:
    """Decorator to add remote connection options to a CLI command.

    Injects ``--remote-url`` and ``--remote-api-key`` and passes them as
    keyword arguments to the wrapped function.
    """
    import functools

    @REMOTE_URL_OPTION
    @REMOTE_API_KEY_OPTION
    @functools.wraps(func)
    def wrapper(
        remote_url: str | None,
        remote_api_key: str | None,
        **kwargs: Any,
    ) -> Any:
        return func(remote_url=remote_url, remote_api_key=remote_api_key, **kwargs)

    return wrapper


def get_filesystem(
    remote_url: str | None = None,
    remote_api_key: str | None = None,
) -> NexusFilesystem:
    """Get a remote NexusFilesystem instance.

    Uses resolve_connection() to determine the effective connection target
    when no explicit --remote-url, --config, or --backend=gcs is provided.

    Args:
        remote_url: Nexus server URL (falls back to NEXUS_URL env var).
        remote_api_key: API key (falls back to NEXUS_API_KEY env var).

    Returns:
        NexusFilesystem instance connected to the remote server.
    """
    try:
        from nexus.cli.config import resolve_connection

        # Get profile name from Click context if available
        profile_name = None
        try:
            ctx = click.get_current_context(silent=True)
            if ctx and ctx.obj:
                profile_name = ctx.obj.get("profile")
        except RuntimeError:
            pass

        resolved = resolve_connection(
            remote_url=remote_url,
            remote_api_key=remote_api_key,
            profile_name=profile_name,
        )

        if not resolved.is_remote:
            console.print("[red]Error:[/red] NEXUS_URL or --remote-url is required")
            console.print(
                "[yellow]Hint:[/yellow] export NEXUS_URL=http://your-nexus-server:2026"
                " or use `nexus profile add`"
            )
            sys.exit(ExitCode.CONFIG_ERROR)

        return nexus.connect(
            config={"profile": "remote", "url": resolved.url, "api_key": resolved.api_key}
        )
    except Exception as e:
        console.print(f"[red]Error connecting to Nexus:[/red] {e}")
        sys.exit(ExitCode.UNAVAILABLE)


def get_default_filesystem() -> NexusFilesystem:
    """Get a remote NexusFilesystem using environment variables.

    Used by commands that don't accept backend options (e.g., memory commands).
    Resolves connection via: NEXUS_URL env > active profile. No local fallback.

    Returns:
        NexusFilesystem instance connected to the remote server.
    """
    try:
        from nexus.cli.config import resolve_connection

        resolved = resolve_connection(
            remote_url=os.environ.get("NEXUS_URL"),
            remote_api_key=os.environ.get("NEXUS_API_KEY"),
        )

        if not resolved.is_remote:
            console.print("[red]Error:[/red] NEXUS_URL environment variable is required")
            console.print(
                "[yellow]Hint:[/yellow] export NEXUS_URL=http://your-nexus-server:2026"
                " or use `nexus profile add`"
            )
            sys.exit(ExitCode.CONFIG_ERROR)

        return nexus.connect(
            config={
                "profile": "remote",
                "url": resolved.url,
                "api_key": resolved.api_key,
            }
        )
    except Exception as e:
        console.print(f"[red]Error connecting to Nexus:[/red] {e}")
        sys.exit(ExitCode.UNAVAILABLE)


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
        sys.exit(ExitCode.USAGE_ERROR)

    parts = subject_str.split(":", 1)
    return (parts[0], parts[1])


def get_zone_id(zone_id: str | None) -> str | None:
    """Get zone ID from parameter or environment.

    Args:
        zone_id: Zone ID from CLI parameter (or NEXUS_ZONE_ID via Click envvar)

    Returns:
        Zone ID or None
    """
    return zone_id or None


def create_operation_context(
    subject: str | None = None,
    zone_id: str | None = None,
    is_admin: bool = False,
    is_system: bool = False,
    admin_capabilities: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Create operation context from CLI parameters.

    This creates a context dict that can be passed to NexusFS operations.
    The dict format is compatible with OperationContext.

    Args:
        subject: Subject string in format 'type:id'
        zone_id: Zone ID
        is_admin: Whether operation has admin privileges
        is_system: Whether operation is system-level
        admin_capabilities: Admin capabilities

    Returns:
        Context dictionary
    """
    # Parse subject
    subject_tuple = parse_subject(subject)

    # Get zone_id
    zone_id = get_zone_id(zone_id)

    # Build context
    context: dict[str, Any] = {}

    if subject_tuple:
        context["subject"] = subject_tuple

    if zone_id:
        context["zone"] = zone_id  # Zone ID for multi-zone isolation

    if is_admin:
        context["is_admin"] = True

    if is_system:
        context["is_system"] = True

    if admin_capabilities:
        context["admin_capabilities"] = set(admin_capabilities)

    return context


def add_context_options(func: Any) -> Any:
    """Decorator to add enhanced context options to a command.

    Adds --subject, --zone-id, --is-admin, --is-system, --admin-capability
    options to commands.
    """
    import functools

    @ADMIN_CAPABILITIES_OPTION
    @IS_SYSTEM_OPTION
    @IS_ADMIN_OPTION
    @ZONE_ID_OPTION
    @SUBJECT_OPTION
    @functools.wraps(func)
    def wrapper(
        subject: str | None,
        zone_id: str | None,
        is_admin: bool,
        is_system: bool,
        admin_capabilities: tuple[str, ...],
        **kwargs: Any,
    ) -> Any:
        # Create context and pass to function
        context = create_operation_context(
            subject=subject,
            zone_id=zone_id,
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
    """Handle errors with beautiful output and semantic exit codes.

    Exit codes follow sysexits.h (POSIX) conventions:
    - 0:  Success (not used here)
    - 64: Usage error (bad input / validation)
    - 66: Not found (file / resource)
    - 69: Unavailable (connection error)
    - 70: Internal error (unexpected)
    - 75: Temporary failure (timeout)
    - 77: Permission denied
    """
    from nexus.contracts.exceptions import AccessDeniedError, NexusPermissionError

    if isinstance(e, PermissionError | AccessDeniedError | NexusPermissionError):
        console.print(f"[red]Permission Denied:[/red] {e}")
        sys.exit(ExitCode.PERMISSION_DENIED)
    elif isinstance(e, NexusFileNotFoundError):
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(ExitCode.NOT_FOUND)
    elif isinstance(e, ValidationError):
        console.print(f"[red]Validation Error:[/red] {e}")
        sys.exit(ExitCode.USAGE_ERROR)
    elif isinstance(e, TimeoutError):
        console.print(f"[red]Timeout:[/red] {e}")
        sys.exit(ExitCode.TEMPFAIL)
    elif isinstance(e, ConnectionError | OSError):
        console.print(f"[red]Connection Error:[/red] {e}")
        sys.exit(ExitCode.UNAVAILABLE)
    elif isinstance(e, NexusError):
        console.print(f"[red]Nexus Error:[/red] {e}")
        sys.exit(ExitCode.GENERAL_ERROR)
    else:
        console.print(f"[red]Unexpected error:[/red] {e}")
        sys.exit(ExitCode.INTERNAL_ERROR)


def resolve_content(content: str | None, input_file: Any) -> bytes:
    """Resolve content from CLI argument, file, or stdin.

    Args:
        content: Content string from CLI argument, or "-" for stdin.
        input_file: File object from ``--input`` option.

    Returns:
        Content as bytes.

    Raises:
        SystemExit: If no content source is provided.
    """
    if input_file:
        return bytes(input_file.read())
    if content == "-":
        return sys.stdin.buffer.read()
    if content:
        return content.encode("utf-8")
    console.print("[red]Error:[/red] Must provide content or use --input")
    sys.exit(ExitCode.USAGE_ERROR)


@contextmanager
def open_filesystem(
    remote_url: str | None = None,
    remote_api_key: str | None = None,
) -> Generator[NexusFilesystem, None, None]:
    """Context manager that opens and auto-closes a NexusFilesystem.

    Usage::

        with open_filesystem(remote_url, remote_api_key) as nx:
            result = nx.sys_readdir(path)
        # nx.close() is called automatically, even on exception.
    """
    nx = get_filesystem(remote_url, remote_api_key)
    try:
        yield nx
    finally:
        nx.close()


# =============================================================================
# JSON output helpers (Issue #2811)
# =============================================================================

# DEPRECATED: Use add_output_options + render_output from nexus.cli.output instead.
# Kept for backwards compatibility with existing commands.
JSON_OUTPUT_OPTION = click.option("--json", "json_output", is_flag=True, help="Output as JSON")


# DEPRECATED: Use add_output_options + render_output from nexus.cli.output instead.
# Kept for backwards compatibility with existing commands.
def output_result(data: Any, json_output: bool, rich_fn: Any) -> None:
    """Output data as JSON or rich-formatted text.

    Args:
        data: The data to output.
        json_output: If True, output as JSON.
        rich_fn: Callable that renders data using Rich (called with data).
    """
    if json_output:
        import json as json_mod

        console.print(json_mod.dumps(data, indent=2, default=str))
    else:
        rich_fn(data)


def get_service_client(
    remote_url: str | None = None,
    remote_api_key: str | None = None,
) -> Any:
    """Create a NexusServiceClient from URL/API key, with validation.

    Args:
        remote_url: Server URL (from --remote-url or NEXUS_URL env var)
        remote_api_key: API key (from --remote-api-key or NEXUS_API_KEY env var)

    Returns:
        NexusServiceClient instance

    Raises:
        SystemExit: If URL is not provided
    """
    if not remote_url:
        console.print("[red]Error:[/red] Server URL required. Set NEXUS_URL or use --remote-url")
        sys.exit(ExitCode.CONFIG_ERROR)

    from nexus.cli.client import NexusServiceClient

    return NexusServiceClient(url=remote_url, api_key=remote_api_key)

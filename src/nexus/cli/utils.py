"""CLI utilities - Common helpers for Nexus CLI commands."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, cast

import click

import nexus
from nexus.cli.exit_codes import ExitCode
from nexus.cli.theme import console, print_error
from nexus.contracts.exceptions import NexusError, NexusFileNotFoundError, ValidationError
from nexus.core.nexus_fs import NexusFS

_LOCAL_WORKSPACE_ENV_KEYS = (
    "NEXUS_URL",
    "NEXUS_API_KEY",
    "NEXUS_PROFILE",
    "NEXUS_BACKEND",
    "NEXUS_GCS_BUCKET_NAME",
    "NEXUS_GCS_PROJECT_ID",
    "NEXUS_GCS_CREDENTIALS_PATH",
    "NEXUS_DB_PATH",
    "NEXUS_METASTORE_PATH",
    "NEXUS_RECORD_STORE_PATH",
    "NEXUS_DATABASE_URL",
    "POSTGRES_URL",
    "DATABASE_URL",
    "NEXUS_HOSTNAME",
    "NEXUS_BIND_ADDR",
    "NEXUS_ADVERTISE_ADDR",
    "NEXUS_GRPC_PORT",
    "NEXUS_PEERS",
    "NEXUS_FEDERATION_ZONES",
    "NEXUS_FEDERATION_MOUNTS",
    "NEXUS_TIMEOUT",
    "NEXUS_ZONE_ID",
    "NEXUS_USER_ID",
    "NEXUS_AGENT_ID",
    "NEXUS_SUBJECT",
    "NEXUS_SUBJECT_TYPE",
    "NEXUS_SUBJECT_ID",
    "NEXUS_READ_REPLICA_URL",
    "TOKEN_MANAGER_DB",
    "CLOUD_SQL_INSTANCE",
    "CLOUD_SQL_READ_INSTANCE",
    "CLOUD_SQL_USER",
    "CLOUD_SQL_DB",
)

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

ADMIN_BACKEND_FEATURES_OPTION = click.option(
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


def _apply_common_config(
    config: dict[str, Any],
    *,
    enforce_permissions: bool | None,
    allow_admin_bypass: bool | None,
    enforce_zone_isolation: bool | None,
    enable_memory_paging: bool,
    memory_main_capacity: int,
    memory_recall_max_age_hours: float,
) -> None:
    """Apply common configuration options to a config dict (mutates in place)."""
    if enforce_permissions is not None:
        config["enforce_permissions"] = enforce_permissions
    if allow_admin_bypass is not None:
        config["allow_admin_bypass"] = allow_admin_bypass
    if enforce_zone_isolation is not None:
        config["enforce_zone_isolation"] = enforce_zone_isolation
    config["enable_memory_paging"] = enable_memory_paging
    config["memory_main_capacity"] = memory_main_capacity
    config["memory_recall_max_age_hours"] = memory_recall_max_age_hours


@contextmanager
def _isolated_local_workspace_env(data_dir: str) -> Generator[None, None, None]:
    """Temporarily mask ambient Nexus connection env for local quickstart use."""
    previous: dict[str, str | None] = {
        key: os.environ.get(key) for key in _LOCAL_WORKSPACE_ENV_KEYS
    }
    previous_data_dir = os.environ.get("NEXUS_DATA_DIR")
    try:
        for key in _LOCAL_WORKSPACE_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["NEXUS_DATA_DIR"] = data_dir
        yield
    finally:
        if previous_data_dir is None:
            os.environ.pop("NEXUS_DATA_DIR", None)
        else:
            os.environ["NEXUS_DATA_DIR"] = previous_data_dir
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _LocalWorkspaceFilesystemProxy:
    """Reapply local-workspace env isolation around filesystem operations."""

    def __init__(self, data_dir: str, filesystem: NexusFS) -> None:
        self._data_dir = data_dir
        self._filesystem = filesystem

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._filesystem, name)
        if not callable(attr):
            return attr

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            with _isolated_local_workspace_env(self._data_dir):
                return attr(*args, **kwargs)

        return _wrapped


async def connect_local_workspace(data_dir: str) -> NexusFS:
    """Connect to a self-contained local workspace without ambient env bleed."""
    with _isolated_local_workspace_env(data_dir):
        filesystem = await nexus.connect(
            config={
                "profile": "slim",
                "backend": "local",
                "data_dir": data_dir,
                "db_path": None,
                "metastore_path": None,
                "record_store_path": None,
                "url": None,
                "api_key": None,
            }
        )
    return cast(NexusFS, _LocalWorkspaceFilesystemProxy(data_dir, filesystem))


async def get_filesystem(
    remote_url: str | None = None,
    remote_api_key: str | None = None,
    *,
    allow_local_default: bool = False,
) -> NexusFS:
    """Get a NexusFS instance.

    Uses resolve_connection() to determine the effective connection target
    when no explicit ``--remote-url`` is provided.

    Args:
        remote_url: Nexus server URL (falls back to NEXUS_URL env var).
        remote_api_key: API key (falls back to NEXUS_API_KEY env var).
        allow_local_default: When True, fall back to a verified local minimal
            profile using ``NEXUS_DATA_DIR`` or ``~/.nexus/data``.

    Returns:
        NexusFS instance.
    """
    try:
        from click.core import ParameterSource

        from nexus.cli.config import resolve_connection

        explicit_local_data_dir = os.environ.get("NEXUS_DATA_DIR")
        remote_url_source = None

        # Get profile name from Click context if available
        profile_name = None
        try:
            ctx = click.get_current_context(silent=True)
            if ctx:
                if ctx.obj:
                    profile_name = ctx.obj.get("profile")
                remote_url_source = ctx.get_parameter_source("remote_url")
        except RuntimeError:
            pass

        # Source-checkout quickstart: if a local data dir is explicitly set and
        # the user did not explicitly request REMOTE mode, prefer the local
        # workspace over ambient config. This preserves the documented local
        # quickstart while still honoring containerized workflows that set
        # NEXUS_PROFILE=remote and pass NEXUS_URL via environment.
        remote_profile_requested = (
            os.environ.get("NEXUS_PROFILE") or ""
        ).strip().lower() == "remote" or profile_name == "remote"
        if (
            allow_local_default
            and explicit_local_data_dir
            and not remote_profile_requested
            and remote_url_source is not ParameterSource.COMMANDLINE
        ):
            return await connect_local_workspace(explicit_local_data_dir)

        resolved = resolve_connection(
            remote_url=remote_url,
            remote_api_key=remote_api_key,
            profile_name=profile_name,
        )

        if not resolved.is_remote:
            if allow_local_default:
                data_dir = os.environ.get(
                    "NEXUS_DATA_DIR",
                    str(Path(nexus.NEXUS_STATE_DIR) / "data"),
                )
                return await nexus.connect(config={"profile": "slim", "data_dir": data_dir})

            console.print("[nexus.error]Error:[/nexus.error] NEXUS_URL or --remote-url is required")
            console.print(
                "[nexus.warning]Hint:[/nexus.warning] export NEXUS_URL=http://your-nexus-server:2026"
                " or use `nexus profile add`"
            )
            sys.exit(ExitCode.CONFIG_ERROR)

        return await nexus.connect(
            config={"profile": "remote", "url": resolved.url, "api_key": resolved.api_key}
        )
    except Exception as e:
        console.print(f"[nexus.error]Error connecting to Nexus:[/nexus.error] {e}")
        sys.exit(ExitCode.UNAVAILABLE)


async def get_default_filesystem() -> NexusFS:
    """Get a remote NexusFS using environment variables.

    Used by commands that don't accept backend options (e.g., memory commands).
    Resolves connection via: NEXUS_URL env > active profile. No local fallback.

    Returns:
        NexusFS instance connected to the remote server.
    """
    try:
        from nexus.cli.config import resolve_connection

        resolved = resolve_connection(
            remote_url=os.environ.get("NEXUS_URL"),
            remote_api_key=os.environ.get("NEXUS_API_KEY"),
        )

        if not resolved.is_remote:
            console.print(
                "[nexus.error]Error:[/nexus.error] NEXUS_URL environment variable is required"
            )
            console.print(
                "[nexus.warning]Hint:[/nexus.warning] export NEXUS_URL=http://your-nexus-server:2026"
                " or use `nexus profile add`"
            )
            sys.exit(ExitCode.CONFIG_ERROR)

        return await nexus.connect(
            config={
                "profile": "remote",
                "url": resolved.url,
                "api_key": resolved.api_key,
            }
        )
    except Exception as e:
        console.print(f"[nexus.error]Error connecting to Nexus:[/nexus.error] {e}")
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
        console.print(f"[nexus.error]Error:[/nexus.error] Invalid subject format: {subject_str}")
        console.print(
            "[nexus.warning]Expected format:[/nexus.warning] type:id (e.g., 'user:alice', 'agent:bot1')"
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

    @ADMIN_BACKEND_FEATURES_OPTION
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
        print_error("Permission Denied", e)
        sys.exit(ExitCode.PERMISSION_DENIED)
    elif isinstance(e, NexusFileNotFoundError):
        print_error("Error", e)
        sys.exit(ExitCode.NOT_FOUND)
    elif isinstance(e, ValidationError):
        print_error("Validation Error", e)
        sys.exit(ExitCode.USAGE_ERROR)
    elif isinstance(e, TimeoutError):
        print_error("Timeout", e)
        sys.exit(ExitCode.TEMPFAIL)
    elif isinstance(e, ConnectionError | OSError):
        print_error("Connection Error", e)
        sys.exit(ExitCode.UNAVAILABLE)
    elif isinstance(e, NexusError):
        print_error("Nexus Error", e)
        sys.exit(ExitCode.GENERAL_ERROR)
    else:
        print_error("Unexpected error", e)
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
        data = input_file.read()
        return data if isinstance(data, bytes) else data.encode("utf-8")
    if content == "-":
        return sys.stdin.buffer.read()
    if content:
        return content.encode("utf-8")
    console.print("[nexus.error]Error:[/nexus.error] Must provide content or use --input")
    sys.exit(ExitCode.USAGE_ERROR)


@asynccontextmanager
async def open_filesystem(
    remote_url: str | None = None,
    remote_api_key: str | None = None,
    **kwargs: Any,
) -> AsyncGenerator[NexusFS, None]:
    """Async context manager that opens and auto-closes a NexusFS.

    Usage::

        async with open_filesystem(remote_url, remote_api_key) as nx:
            result = nx.sys_readdir(path)
        # nx.close() is called automatically, even on exception.
    """
    nx = await get_filesystem(remote_url, remote_api_key, **kwargs)
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


def rpc_call(
    remote_url: str | None,
    remote_api_key: str | None,
    rpc_method: str,
    **kwargs: Any,
) -> Any:
    """Execute a service RPC via gRPC REMOTE profile.

    Uses the same RemoteServiceProxy + RPCTransport path that filesystem
    commands use. Any method name dispatches to server dispatch_method()
    via gRPC Call RPC.
    """
    import asyncio

    method_aliases = {
        "federation_list_zones": "federation_zones",
    }
    method_name = method_aliases.get(rpc_method, rpc_method)

    async def _call() -> Any:
        nx = await get_filesystem(remote_url, remote_api_key)
        try:
            proxy = nx.service("operations")
            if proxy is None:
                raise RuntimeError("Not connected in REMOTE mode")
            return getattr(proxy, method_name)(**kwargs)
        finally:
            nx.close()

    return asyncio.run(_call())

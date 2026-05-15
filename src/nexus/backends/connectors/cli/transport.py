"""CLI Transport — raw key-based I/O over CLI subprocess commands.

Implements the Transport protocol for CLI-backed connectors, mapping:
- fetch(key) → CLI get command → bytes
- list_keys(prefix) → CLI list command → keys
- exists(key) → CLI get (check exit code)
- store(key, data) → CLI write command → bytes on stdin
- remove(key) → not supported (CLI connectors are typically read-only)

The CLITransport wraps a CLIProtocol-compatible connector (any object with
_execute_cli, CLI_NAME, CLI_SERVICE, _config, _get_user_token, _build_auth_env)
and exposes Transport-protocol methods.

Unlike OAuth transports (GmailTransport, CalendarTransport), CLITransport:
- Does NOT use OAuth tokens — uses subprocess/CLI commands for I/O
- Does NOT need with_context() — CLI auth is via env vars, not per-user OAuth
- Delegates all subprocess execution to the existing _execute_cli machinery

Key schema:
    Keys are CLI-relative paths. The CLITransport maps them to CLI args
    via the CLIConnectorConfig read/write configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.connectors.cli.result import CLIResult, CLIResultStatus
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.backends.connectors.cli.config import CLIConnectorConfig
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class CLITransport:
    """CLI subprocess transport implementing the Transport protocol.

    Wraps the CLI execution machinery (_execute_cli, config-based commands)
    to expose a Transport-compatible interface for PathAddressingEngine.

    Attributes:
        transport_name: ``"cli"`` — used by PathAddressingEngine to build
            the backend name (``"path-cli"`` or overridden by caller).
    """

    transport_name: str = "cli"

    def __init__(
        self,
        cli_name: str,
        cli_service: str = "",
        config: "CLIConnectorConfig | None" = None,
        execute_cli_fn: Any = None,
        get_user_token_fn: Any = None,
        build_auth_env_fn: Any = None,
    ) -> None:
        """Initialize CLITransport.

        Args:
            cli_name: CLI binary name (e.g., 'gws', 'gh').
            cli_service: Service within the CLI (e.g., 'gmail', 'issue').
            config: CLIConnectorConfig for command routing.
            execute_cli_fn: Callable matching _execute_cli signature.
            get_user_token_fn: Callable to resolve user token from context.
            build_auth_env_fn: Callable to build auth env vars from token.
        """
        self.cli_name = cli_name
        self.cli_service = cli_service
        self._config = config
        self._execute_cli = execute_cli_fn or self._default_execute_cli
        self._get_user_token = get_user_token_fn
        self._build_auth_env = build_auth_env_fn
        self.transport_name = f"cli:{cli_name}:{cli_service}" if cli_service else f"cli:{cli_name}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_auth_env(self, context: "OperationContext | None" = None) -> dict[str, str] | None:
        """Resolve auth env vars from context."""
        if self._get_user_token and self._build_auth_env:
            token = self._get_user_token(context)
            if token:
                return dict(self._build_auth_env(token))
        return None

    @staticmethod
    def _default_execute_cli(
        args: list[str],
        stdin: str | None = None,
        context: "OperationContext | None" = None,  # noqa: ARG004
        env: dict[str, str] | None = None,
    ) -> CLIResult:
        """Default CLI execution via subprocess."""
        import os
        import shutil
        import subprocess
        import time

        if not args:
            return CLIResult(
                status=CLIResultStatus.NOT_INSTALLED,
                command=args,
            )

        cli_binary = args[0]
        if shutil.which(cli_binary) is None:
            return CLIResult(
                status=CLIResultStatus.NOT_INSTALLED,
                command=args,
                stderr=f"CLI '{cli_binary}' not found in PATH",
            )

        needs_custom_env = bool(env) or "GOOGLE_APPLICATION_CREDENTIALS" in os.environ
        _nexus_home = "/home/nexus"
        _cli_lower = (cli_binary or "").lower()
        if _cli_lower in ("gws", "gh") and os.path.isdir(f"{_nexus_home}/.config"):
            needs_custom_env = True

        run_env: dict[str, str] | None = None
        if needs_custom_env:
            run_env = {**os.environ}
            run_env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            if env:
                run_env.update(env)
            if (
                _cli_lower in ("gws", "gh")
                and run_env.get("HOME") != _nexus_home
                and os.path.isdir(f"{_nexus_home}/.config")
            ):
                run_env["HOME"] = _nexus_home

        start = time.perf_counter()
        try:
            proc = subprocess.run(
                args,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=60,
                env=run_env,
            )
            duration_ms = (time.perf_counter() - start) * 1000

            if proc.returncode == 0:
                return CLIResult(
                    status=CLIResultStatus.SUCCESS,
                    exit_code=0,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    command=args,
                    duration_ms=duration_ms,
                )
            return CLIResult(
                status=CLIResultStatus.EXIT_ERROR,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                command=args,
                duration_ms=duration_ms,
            )

        except subprocess.TimeoutExpired:
            duration_ms = (time.perf_counter() - start) * 1000
            return CLIResult(
                status=CLIResultStatus.TIMEOUT,
                command=args,
                duration_ms=duration_ms,
                stderr="Command timed out after 60s",
            )
        except Exception as e:
            duration_ms = (time.perf_counter() - start) * 1000
            return CLIResult(
                status=CLIResultStatus.EXIT_ERROR,
                command=args,
                duration_ms=duration_ms,
                stderr=str(e),
            )

    # ------------------------------------------------------------------
    # Transport protocol methods
    # ------------------------------------------------------------------

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Store content via CLI write command.

        Builds CLI args from config write operations, sends data as YAML
        on stdin. Returns None (CLI doesn't provide version IDs).
        """
        if not self._config or not self._config.write:
            raise BackendError(
                "CLI transport has no write operations configured.",
                backend=self.transport_name,
            )

        # Find the write operation matching the key path
        operation = None
        write_op = None
        key_stripped = key.strip("/")
        for wop in self._config.write:
            if key_stripped.endswith(wop.path):
                operation = wop.operation
                write_op = wop
                break

        if operation is None or write_op is None:
            raise BackendError(
                f"No write operation mapped to key: {key}",
                backend=self.transport_name,
            )

        args = [self.cli_name]
        if self.cli_service:
            args.append(self.cli_service)
        args.extend(write_op.command.split())

        # Send data as YAML on stdin
        stdin_data: str | None = data.decode("utf-8") if isinstance(data, bytes) else str(data)

        auth_env = self._get_auth_env()
        result = self._execute_cli(args, stdin=stdin_data, env=auth_env)

        if not result.ok:
            raise BackendError(
                f"CLI store failed: {result.summary()}",
                backend=self.transport_name,
            )

        return None

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Fetch content via CLI get command."""
        if not self._config or not self._config.read:
            raise NexusFileNotFoundError(key)

        args = [self.cli_name]
        if self.cli_service:
            args.append(self.cli_service)
        args.append(self._config.read.get_command)
        args.append(key)

        auth_env = self._get_auth_env()
        result = self._execute_cli(args, env=auth_env)

        if not result.ok:
            raise NexusFileNotFoundError(key)

        return result.stdout.encode("utf-8"), None

    def remove(self, key: str) -> None:
        """Remove content — not typically supported by CLI connectors."""
        raise BackendError(
            "CLI transport does not support content removal.",
            backend=self.transport_name,
        )

    def exists(self, key: str) -> bool:
        """Check whether a key exists by attempting to fetch it."""
        if not self._config or not self._config.read:
            return False

        args = [self.cli_name]
        if self.cli_service:
            args.append(self.cli_service)
        args.append(self._config.read.get_command)
        args.append(key)

        auth_env = self._get_auth_env()
        result = self._execute_cli(args, env=auth_env)
        return result.ok

    def get_size(self, key: str) -> int:
        """Return content size by fetching and measuring."""
        content, _ = self.fetch(key)
        return len(content)

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List keys under prefix via CLI list command.

        Returns (blob_keys, common_prefixes) following S3/GCS semantics.
        """
        if not self._config or not self._config.read:
            return [], []

        args = [self.cli_name]
        if self.cli_service:
            args.append(self.cli_service)
        args.append(self._config.read.list_command)

        prefix_stripped = prefix.strip("/") if prefix else ""
        if prefix_stripped and prefix_stripped != "/":
            args.append(prefix_stripped)

        auth_env = self._get_auth_env()
        result = self._execute_cli(args, env=auth_env)

        if not result.ok:
            return [], []

        # Parse output based on config format
        try:
            fmt = self._config.read.format if self._config.read else "text"
            if fmt == "json":
                import json

                data = json.loads(result.stdout)
                if isinstance(data, list):
                    return [str(item) for item in data], []
                if isinstance(data, dict):
                    return list(data.keys()), []
                return [], []
            elif fmt == "yaml":
                data = yaml.safe_load(result.stdout)
                if isinstance(data, list):
                    return [str(item) for item in data], []
                if isinstance(data, dict):
                    return list(data.keys()), []
                return [], []
            else:
                lines = [line for line in result.stdout.strip().split("\n") if line.strip()]
                return lines, []
        except Exception:
            logger.debug("Failed to parse CLI list output", exc_info=True)
            return [], []

    def copy_key(self, src_key: str, dst_key: str) -> None:
        """Copy a key — not supported by CLI transport."""
        raise BackendError(
            "CLI transport does not support copy.",
            backend=self.transport_name,
        )

    def create_dir(self, key: str) -> None:
        """Create directory — CLI connectors use virtual directories."""
        # No-op: CLI connectors don't have real directories
        pass

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream content (small payloads — fetch then chunk)."""
        data, _ = self.fetch(key, version_id)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def store_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        """Write from chunks — assemble then store."""
        data = b"".join(chunks)
        return self.store(key, data, content_type)

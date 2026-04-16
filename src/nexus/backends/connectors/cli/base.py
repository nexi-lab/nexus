"""PathCLIBackend — PathAddressingEngine + CLITransport composition.

Refactored from the original CLIConnector to follow the same Transport x
PathAddressingEngine composition pattern as PathGmailBackend, PathCalendarBackend,
and PathGDriveBackend.

Architecture:
    PathCLIBackend(PathAddressingEngine)
        +-- CLITransport(Transport)
              +-- subprocess execution (CLI I/O)
              +-- env-var auth (no OAuth)

The CLITransport handles raw key->bytes I/O via CLI subprocess commands.
PathAddressingEngine handles addressing, path security, and content operations.

Design decisions (Issue #3148):
    - Implements write_content() with context.backend_path (same as Calendar)
    - Schema validation + trait enforcement via existing mixins
    - Per-user, per-zone OAuth via TokenManager + OperationContext
    - Async subprocess spec defined (2A+C), implementation in this module
    - Token piped via stdin, never CLI args (Decision #2)
    - CLIErrorMapper for structured error classification (Decision #6A)
    - Leans on TokenManager for caching/refresh (Decision #16A)

Phase 2 deliverable -- concrete connectors (Gmail, GitHub) come in Phase 3+.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.connectors.base import (
    CheckpointMixin,
    ReadmeDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.backends.connectors.cli.config import CLIConnectorConfig
from nexus.backends.connectors.cli.display_path import DisplayPathMixin
from nexus.backends.connectors.cli.result import CLIErrorMapper, CLIResult, CLIResultStatus
from nexus.backends.connectors.cli.transport import CLITransport
from nexus.contracts.backend_features import (
    CLI_BACKEND_FEATURES,
    OAUTH_BACKEND_FEATURES,
    BackendFeature,
)
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.backends.base.backend import HandlerStatusResponse
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class PathCLIBackend(
    PathAddressingEngine,
    ReadmeDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
    CheckpointMixin,
    DisplayPathMixin,
):
    """PathAddressingEngine + CLITransport composition for CLI-backed connectors.

    Subclasses configure via class attributes or a ``CLIConnectorConfig``.
    The base class handles:

    - ``write_content()``: YAML parse -> schema validate -> trait check -> CLI exec
    - ``check_connection()``: Verify CLI is installed and accessible
    - ``list_dir()``: Delegate to CLI list command
    - ``read_content()``: Delegate to CLI get command via transport
    - Skill doc generation (via ReadmeDocMixin)
    - Error mapping (via CLIErrorMapper)
    - Token resolution (via TokenManager + OperationContext)

    Subclasses must implement:

    - ``name`` property: Backend identifier (or rely on default)

    Example::

        class GmailCLIConnector(PathCLIBackend):
            SKILL_NAME = "gmail"
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            # ... SCHEMAS, OPERATION_TRAITS, etc.
    """

    # --- Class-level configuration (override in subclasses) ---

    CLI_NAME: str = ""
    """CLI binary name (e.g., 'gws', 'gh')."""

    CLI_SERVICE: str = ""
    """Service within the CLI (e.g., 'gmail', 'issue')."""

    AUTH_SOURCE: str | None = None
    """External CLI auth source, e.g., 'gws-cli', 'gh-cli', 'gcloud'.

    When set, _get_user_token() first tries to resolve a credential through
    the unified profile store (via ``_external_sync_boot.resolve_token_for_provider``),
    falling back to TokenManager only if no matching profile exists.
    Phase 3, #3740.
    """

    # Auth is ALWAYS via environment variables (never CLI flags).
    # See _build_auth_env() for the mapping.

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = (
        CLI_BACKEND_FEATURES | OAUTH_BACKEND_FEATURES
    )

    # --- Instance state ---

    def __init__(
        self,
        config: CLIConnectorConfig | None = None,
        token_manager_db: str | None = None,
        **kwargs: Any,
    ) -> None:
        # Fall back to class-level _DEFAULT_CONFIG for subclasses created
        # by create_connector_class_from_yaml() (Phase 5 config dir loading).
        if config is None:
            config = getattr(self.__class__, "_DEFAULT_CONFIG", None)

        self._config = config
        self._token_manager_db = token_manager_db
        self._token_manager: Any = None

        # Initialize TokenManager from database URL if provided.
        # Follows the same pattern as OAuthConnectorMixin._init_oauth().
        if token_manager_db:
            try:
                import importlib as _il

                TokenManager = _il.import_module(
                    "nexus.bricks.auth.oauth.token_manager"
                ).TokenManager
                from nexus.backends.connectors.utils import resolve_database_url

                resolved_db = resolve_database_url(token_manager_db)
                if resolved_db.startswith(("postgresql://", "sqlite://", "mysql://")):
                    self._token_manager = TokenManager(db_url=resolved_db)
                else:
                    self._token_manager = TokenManager(db_path=resolved_db)
                logger.debug("TokenManager initialized for CLI connector %s", self.CLI_NAME)
            except Exception:
                logger.debug("TokenManager not available for %s", self.CLI_NAME, exc_info=True)

        # Error mapper -- uses config's custom patterns if available
        extra_patterns = None
        if config and config.error_patterns:
            from nexus.backends.connectors.cli.result import ErrorMapping

            extra_patterns = [
                (p["pattern"], ErrorMapping(code=p["code"], retryable=p.get("retryable", False)))
                for p in config.error_patterns
                if "pattern" in p and "code" in p
            ]
        self._error_mapper = CLIErrorMapper(extra_patterns=extra_patterns)

        # Apply config values only if the subclass didn't explicitly set them.
        # We check __dict__ to distinguish "subclass set CLI_SERVICE = ''" from
        # "inherited the default ''" -- GitHubConnector explicitly sets "" to mean
        # "no service subcommand".
        if config:
            if "CLI_NAME" not in type(self).__dict__:
                self.CLI_NAME = config.cli
            if "CLI_SERVICE" not in type(self).__dict__:
                self.CLI_SERVICE = config.service

        # Initialize CheckpointMixin state (cooperative __init__ is bypassed
        # because we call PathAddressingEngine.__init__ explicitly below)
        self._checkpoints: dict[str, Any] = {}

        # Create CLITransport with our execution machinery
        cli_transport = CLITransport(
            cli_name=self.CLI_NAME,
            cli_service=self.CLI_SERVICE,
            config=config,
            execute_cli_fn=self._execute_cli,
            get_user_token_fn=self._get_user_token,
            build_auth_env_fn=self._build_auth_env,
        )
        self._cli_transport = cli_transport

        # Initialize PathAddressingEngine with the CLITransport
        backend_name = (
            f"cli:{self.CLI_NAME}:{self.CLI_SERVICE}"
            if self.CLI_SERVICE
            else f"cli:{self.CLI_NAME}"
        )
        PathAddressingEngine.__init__(
            self,
            transport=cli_transport,
            backend_name=backend_name,
        )

    # --- Backend identity ---

    @property
    def name(self) -> str:
        """Backend identifier."""
        return self._backend_name

    @property
    def supports_external_content(self) -> bool:
        return True

    # --- Connection lifecycle ---

    def check_connection(
        self, context: "OperationContext | None" = None
    ) -> "HandlerStatusResponse":
        """Check if CLI is available."""
        import shutil

        from nexus.backends.base.backend import HandlerStatusResponse

        if not self.CLI_NAME:
            return HandlerStatusResponse(
                success=False,
                error_message="CLI_NAME not configured",
            )

        cli_path = shutil.which(self.CLI_NAME)
        if cli_path is None:
            return HandlerStatusResponse(
                success=False,
                error_message=f"CLI '{self.CLI_NAME}' not found in PATH",
            )

        return HandlerStatusResponse(
            success=True,
            details={"cli": self.CLI_NAME, "path": cli_path},
        )

    # --- Token resolution ---
    # Phase 3 (#3740): two-phase resolution.
    #   1. If AUTH_SOURCE is set and CredentialPoolRegistry + ExternalCliBackend
    #      are available, try the external-CLI path (unified profile store).
    #   2. Fall back to TokenManager (existing behavior, Decision #16A).

    def _get_user_token(self, context: "OperationContext | None" = None) -> str | None:
        """Resolve an auth token via external-CLI first, TokenManager as fallback.

        Phase 1 (Phase 3 of #3722, issue #3740): if AUTH_SOURCE is set *and*
        the request is not zone-scoped, try
        ``_external_sync_boot.resolve_token_for_provider`` which selects a
        profile from the unified store and routes through the matching adapter.

        Phase 2 (existing): TokenManager.get_credentials() scoped by user+zone.

        Returns None if neither path yields a token (allows CLI to use
        its own auth, e.g., `gh auth login`).

        **Zone-scope safety:** the external-CLI profile store is host-global
        — it has no concept of zones yet. Using it to satisfy a zone-scoped
        request would leak the host's active CLI identity into a zone that
        expected an isolated credential. Until the profile store becomes
        zone-aware (sister epic: PostgresAuthProfileStore), Phase 1 is
        skipped whenever ``context.zone_id`` is set to anything other than
        the root zone sentinel.
        """
        # Phase 1: external-CLI credential via unified profile store (#3740).
        # Gated on root-or-unscoped zone to preserve zone-scoped token safety.
        if self.AUTH_SOURCE and self._is_zone_safe_for_external_cli(context):
            token = self._resolve_from_external_cli(context)
            if token:
                return token

        # Phase 2: TokenManager (existing behavior, unchanged)
        if self._token_manager is None:
            return None
        if context is None:
            return None

        try:
            user_email = getattr(context, "user_id", None)
            zone_id = getattr(context, "zone_id", None)
            if not user_email:
                return None

            provider = "google"  # Default; subclasses override
            if self._config and self._config.auth:
                provider = self._config.auth.provider

            credentials = self._token_manager.get_credentials(
                user_email=user_email,
                provider=provider,
                zone_id=zone_id,
            )
            if credentials:
                return str(credentials.get("access_token", ""))
        except Exception:
            logger.debug("Token resolution failed for %s", context.user_id, exc_info=True)

        return None

    @staticmethod
    def _is_zone_safe_for_external_cli(context: "OperationContext | None") -> bool:
        """Return True when it's safe to consult the host-global external-CLI store.

        External-CLI profiles are host-global (machine-local CLI logins shared
        across the whole process). Zone-scoped requests require a credential
        that was explicitly provisioned for that zone — mixing the two would
        leak the machine's CLI identity into a zone that expected isolation.

        We treat *no zone set* and *explicit root zone* as safe; any other
        zone value falls through to TokenManager, which has zone-aware lookup.
        """
        if context is None:
            return True
        zone_id = getattr(context, "zone_id", None)
        if not zone_id:
            return True
        from nexus.contracts.constants import ROOT_ZONE_ID

        return str(zone_id) == ROOT_ZONE_ID

    def _resolve_from_external_cli(self, context: "OperationContext | None" = None) -> str | None:
        """Try to resolve a token via the unified profile store.

        Uses ``_external_sync_boot.resolve_token_for_provider`` — same
        process-wide pattern as aws S3 credential routing (no long-lived
        registry objects to inject).  When ``context.user_id`` is set it
        scopes selection to that account, avoiding cross-user credential
        bleed in multi-tenant deployments.

        Returns None on any failure; ``_get_user_token`` falls back to
        TokenManager in that case.
        """
        provider = "google"  # default
        if self._config and self._config.auth:
            provider = self._config.auth.provider

        account: str | None = None
        if context is not None:
            user_id = getattr(context, "user_id", None)
            if user_id:
                account = str(user_id)

        try:
            from nexus.fs._external_sync_boot import (
                ensure_external_sync,
                resolve_token_for_provider,
            )

            ensure_external_sync()
            return resolve_token_for_provider(provider, account=account)
        except Exception:
            logger.debug("External CLI credential resolution failed", exc_info=True)
            return None

    # --- Write path: YAML -> validate -> traits -> CLI exec ---

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Write content by validating YAML and executing CLI command.

        Follows the same contract as Calendar/Gmail connectors:
        ``context.backend_path`` determines the operation.

        Pipeline: parse YAML -> resolve operation -> validate schema ->
        check traits -> get token -> execute CLI -> return result.
        """
        # Backward compatibility: many existing call sites pass
        # write_content(content, context) positionally.
        if context is None and hasattr(content_id, "backend_path"):
            context = cast("OperationContext", content_id)
            content_id = ""

        if not context or not context.backend_path:
            from nexus.contracts.exceptions import BackendError

            raise BackendError(
                f"{self.name} requires backend_path in OperationContext.",
                backend=self.name,
            )

        path = context.backend_path.strip("/")
        operation = self._resolve_operation(path)

        if operation is None:
            from nexus.contracts.exceptions import BackendError

            raise BackendError(
                f"No operation mapped to path: {path}",
                backend=self.name,
            )

        # Parse YAML -- extract comment-based metadata from the header block only.
        # Stop at the first non-comment, non-blank line to avoid matching
        # comments inside literal block scalars (e.g. body: |\n  # confirm: true).
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        comment_meta: dict[str, Any] = {}
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                if stripped.startswith("# agent_intent:"):
                    comment_meta["agent_intent"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("# confirm:"):
                    comment_meta["confirm"] = stripped.split(":", 1)[1].strip().lower() == "true"
                elif stripped.startswith("# user_confirmed:"):
                    comment_meta["user_confirmed"] = (
                        stripped.split(":", 1)[1].strip().lower() == "true"
                    )
            else:
                break  # First non-comment line -- stop scanning

        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            from nexus.contracts.exceptions import BackendError

            raise BackendError(
                f"Expected YAML mapping, got {type(data).__name__}",
                backend=self.name,
            )

        # Merge comment metadata (comments take precedence for backward compat)
        for key, val in comment_meta.items():
            if key not in data:
                data[key] = val

        # Validate traits (agent_intent, confirm, etc.)
        warnings = self.validate_traits(operation, data)
        for warning in warnings:
            logger.warning("CLI write warning for %s: %s", operation, warning)

        # Validate schema
        validated = self.validate_schema(operation, data)

        # Create checkpoint for reversible operations
        checkpoint = self.create_checkpoint(operation)

        # Get auth token
        token = self._get_user_token(context)

        # Build CLI command
        cli_args = self._build_cli_args(operation, validated, path)

        # Prepare stdin payload -- hookable so subclasses (e.g., GmailConnector)
        # can return None to use flag-based args instead of stdin YAML.
        payload_yaml = self._prepare_stdin(operation, validated, data)

        # Auth token is passed via CLI-specific transport (env var or flag),
        # NOT mixed into stdin. The payload YAML goes on stdin alone.
        # - gh: GH_TOKEN env var
        # - gws: --access-token flag (added to cli_args)
        auth_env: dict[str, str] = {}
        if token:
            auth_env = self._build_auth_env(token)

        # Execute with payload on stdin and auth via env/flag
        result = self._execute_cli(cli_args, stdin=payload_yaml, context=context, env=auth_env)

        # Classify errors
        result = self._error_mapper.classify_result(result)

        if not result.ok:
            from nexus.contracts.exceptions import BackendError

            error_msg = result.summary()
            if result.retryable:
                error_msg += " (retryable)"
            raise BackendError(error_msg, backend=self.name)

        # Store checkpoint result for potential rollback
        if checkpoint:
            self.complete_checkpoint(
                checkpoint.checkpoint_id,
                created_state={"cli_output": result.stdout[:1000]},
            )

        content_id = hashlib.sha256(result.stdout.encode()).hexdigest()
        return WriteResult(content_id=content_id, size=len(content))

    def _resolve_operation(self, path: str) -> str | None:
        """Map a backend_path to an operation name.

        Uses the write config from CLIConnectorConfig if available,
        otherwise falls back to SCHEMAS keys matching path patterns.
        """
        if self._config:
            for write_op in self._config.write:
                if path.endswith(write_op.path):
                    return write_op.operation

        # Fallback: check if path ends with a known operation pattern
        for op_name in self.SCHEMAS:
            if f"_{op_name}" in path or path.endswith(f"/{op_name}.yaml"):
                return op_name

        return None

    def _build_cli_args(
        self,
        operation: str,
        validated: Any,
        path: str,
    ) -> list[str]:
        """Build CLI command arguments for an operation.

        Args:
            operation: Operation name.
            validated: Validated Pydantic model.
            path: Backend path.

        Returns:
            CLI argument list.
        """
        args = [self.CLI_NAME]

        if self.CLI_SERVICE:
            args.append(self.CLI_SERVICE)

        # Find the command from config -- split multi-word commands into
        # separate argv elements (e.g., "issue create" -> ["issue", "create"])
        if self._config:
            for write_op in self._config.write:
                if write_op.operation == operation:
                    args.extend(write_op.command.split())
                    break

        return args

    def _prepare_stdin(self, operation: str, validated: Any, data: dict) -> str | None:
        """Prepare the stdin payload for CLI execution.

        Default: serialize validated model as YAML for stdin.
        Override to return ``None`` for connectors that pass data via CLI
        flags instead of stdin (e.g., GmailConnector gws helper commands).
        """
        return yaml.dump(
            validated.model_dump(exclude_none=True) if hasattr(validated, "model_dump") else data,
            default_flow_style=False,
        )

    def _build_auth_env(self, token: str) -> dict[str, str]:
        """Build environment variables for CLI auth.

        All auth is via environment variables -- NEVER via CLI flags,
        which would expose tokens in ``ps`` / ``/proc`` output.

        Known CLI env vars:
        - gh: GH_TOKEN
        - gws: GWS_ACCESS_TOKEN (or GOOGLE_ACCESS_TOKEN)
        - Generic fallback: NEXUS_CLI_ACCESS_TOKEN

        Subclasses can override for custom auth transports.
        """
        cli = self.CLI_NAME.lower()
        if cli == "gh":
            return {"GH_TOKEN": token}
        if cli == "gws":
            return {"GWS_ACCESS_TOKEN": token}
        # Generic fallback -- connector-specific env var
        env_key = f"{self.CLI_NAME.upper().replace('-', '_')}_ACCESS_TOKEN"
        return {env_key: token}

    # --- CLI execution (subclasses may override) ---

    def _execute_cli(
        self,
        args: list[str],
        stdin: str | None = None,
        context: "OperationContext | None" = None,
        env: dict[str, str] | None = None,
    ) -> CLIResult:
        """Execute a CLI command.

        Default implementation uses subprocess. Override for testing
        or custom execution strategies.

        Args:
            args: CLI command arguments.
            stdin: Optional stdin input (YAML payload).
            context: Operation context.
            env: Additional environment variables (e.g., auth tokens).

        Returns:
            CLIResult with status, stdout, stderr, exit code.
        """
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

        # Build subprocess environment only when customization is needed.
        # - Strip GOOGLE_APPLICATION_CREDENTIALS (breaks gws OAuth in Docker)
        # - Set HOME=/home/nexus for gws/gh (Docker runs as root)
        # - Merge caller-provided auth env vars
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
            # Docker fix: gws/gh config is at /home/nexus/.config/
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

    # --- Read operations (override PathAddressingEngine for CLI-specific behavior) ---

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read content via CLI get command.

        Auth is via environment variables (never stdin/flags) -- consistent
        with the write path security model.
        """
        if not context or not context.backend_path:
            return b""

        if not self._config or not self._config.read:
            return b""

        args = [self.CLI_NAME]
        if self.CLI_SERVICE:
            args.append(self.CLI_SERVICE)
        args.append(self._config.read.get_command)
        args.append(context.backend_path)

        # Auth via env vars only (never stdin -- tokens visible in /proc on some OS)
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        result = self._execute_cli(args, context=context, env=auth_env)

        if result.ok:
            return result.stdout.encode("utf-8")
        return b""

    def list_dir(
        self,
        path: str = "/",
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """List directory contents via CLI list command.

        Auth is via environment variables (never stdin/flags).
        """
        if not self._config or not self._config.read:
            return []

        args = [self.CLI_NAME]
        if self.CLI_SERVICE:
            args.append(self.CLI_SERVICE)
        args.append(self._config.read.list_command)
        if path and path != "/":
            args.append(path)

        # Auth via env vars only (consistent with write path)
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        result = self._execute_cli(args, context=context, env=auth_env)

        if not result.ok:
            return []

        # Parse output based on format
        try:
            if self._config.read.format == "json":
                import json

                data = json.loads(result.stdout)
                if isinstance(data, list):
                    return [str(item) for item in data]
                return list(data.keys()) if isinstance(data, dict) else []
            elif self._config.read.format == "yaml":
                data = yaml.safe_load(result.stdout)
                if isinstance(data, list):
                    return [str(item) for item in data]
                return list(data.keys()) if isinstance(data, dict) else []
            else:
                return result.stdout.strip().split("\n")
        except Exception:
            logger.debug("Failed to parse CLI list output", exc_info=True)
            return []

    # --- Batch metadata for display_path (optional protocol) ---

    def list_dir_metadata(
        self,
        path: str = "/",
        context: "OperationContext | None" = None,
    ) -> dict[str, dict[str, Any]] | None:
        """Return per-file metadata for all items in a directory (batch).

        Optional protocol for connectors that can fetch metadata for an
        entire directory in a single call (e.g., Gmail ``+triage``,
        Calendar ``+agenda``).  Returns ``{filename: {metadata_dict}}``
        where ``filename`` matches the entries returned by ``list_dir()``.

        Returns ``None`` when not implemented -- the sync service falls
        back to per-file ``read_content`` for ``display_path`` resolution.

        Subclasses override this to eliminate N serial HTTP calls during
        sync by pre-fetching subject/date/labels/etc. in one batch.
        """
        return None

    # --- Stub implementations for Backend ABC ---

    def delete_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """Content deletion not supported via CLI."""

    def content_exists(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        return False

    def get_content_size(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        return 0

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Directories are virtual -- no-op."""

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Directories are virtual -- no-op."""

    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Infer from path pattern: paths without extension are directories."""
        return "." not in path.rsplit("/", 1)[-1]

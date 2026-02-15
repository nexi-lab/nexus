"""Sandbox manager for Nexus-managed sandboxes.

Coordinates sandbox lifecycle management using providers (E2B, Docker, etc.)
and database metadata storage. Handles creation, TTL tracking, and cleanup.

Uses session-per-operation pattern: each DB operation gets a fresh session
from the session_factory, preventing stale identity maps and connection leaks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import PendingRollbackError, SQLAlchemyError
from sqlalchemy.orm import Session

from nexus.sandbox.sandbox_provider import (
    CodeExecutionResult,
    EscalationNeeded,
    SandboxNotFoundError,
    SandboxProvider,
)
from nexus.storage.models import SandboxMetadataModel

# Try to import E2B provider
try:
    from nexus.sandbox.sandbox_e2b_provider import E2BSandboxProvider

    E2B_PROVIDER_AVAILABLE = True
except ImportError:
    E2B_PROVIDER_AVAILABLE = False

# Try to import Docker provider
try:
    from nexus.sandbox.sandbox_docker_provider import DockerSandboxProvider

    DOCKER_PROVIDER_AVAILABLE = True
except ImportError:
    DOCKER_PROVIDER_AVAILABLE = False

# Try to import Monty provider (Issue #1316)
try:
    from nexus.sandbox.sandbox_monty_provider import MontySandboxProvider

    MONTY_PROVIDER_AVAILABLE = True
except ImportError:
    MONTY_PROVIDER_AVAILABLE = False

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class SandboxManager:
    """Manages sandboxes across different providers with database persistence.

    Responsibilities:
    - Create sandboxes using providers (E2B, Docker, etc.)
    - Store metadata in database
    - Track TTL and expiry
    - Handle lifecycle operations (pause/resume/stop)
    - Clean up expired sandboxes

    Note: Providers are async. Database operations use sync sessions (session-per-op).
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        e2b_api_key: str | None = None,
        e2b_team_id: str | None = None,
        e2b_template_id: str | None = None,
        config: Any = None,  # NexusConfig | None
    ):
        """Initialize sandbox manager.

        Args:
            session_factory: Factory that creates fresh DB sessions
            e2b_api_key: E2B API key
            e2b_team_id: E2B team ID
            e2b_template_id: Default E2B template ID
            config: Nexus configuration (for Docker templates)
        """
        self._session_factory = session_factory

        # Initialize providers
        self.providers: dict[str, SandboxProvider] = {}

        # Initialize E2B provider if available and API key provided
        if E2B_PROVIDER_AVAILABLE and e2b_api_key:
            try:
                self.providers["e2b"] = E2BSandboxProvider(
                    api_key=e2b_api_key,
                    team_id=e2b_team_id,
                    default_template=e2b_template_id,
                )
                logger.info("E2B provider initialized successfully")
            except Exception as e:
                logger.warning(f"Failed to initialize E2B provider: {e}")
        elif not E2B_PROVIDER_AVAILABLE and e2b_api_key:
            logger.info("E2B provider not available (e2b_code_interpreter package not installed)")

        # Initialize Docker provider if available (no API key needed)
        if DOCKER_PROVIDER_AVAILABLE:
            try:
                docker_config = config.docker if config and hasattr(config, "docker") else None
                self.providers["docker"] = DockerSandboxProvider(docker_config=docker_config)
                logger.info("Docker provider initialized successfully")
            except RuntimeError as e:
                # RuntimeError from DockerSandboxProvider means Docker daemon not available
                logger.info(f"Docker provider not available: {e}")
            except Exception as e:
                logger.warning(f"Failed to initialize Docker provider: {e}")

        # Initialize Monty provider if available (no external deps, Issue #1316)
        if MONTY_PROVIDER_AVAILABLE:
            try:
                monty_profile = "standard"
                if config and hasattr(config, "monty_resource_profile"):
                    monty_profile = config.monty_resource_profile
                self.providers["monty"] = MontySandboxProvider(
                    resource_profile=monty_profile,
                )
                logger.info("Monty provider initialized successfully")
            except RuntimeError as e:
                logger.info(f"Monty provider not available: {e}")
            except Exception as e:
                logger.warning(f"Failed to initialize Monty provider: {e}")

        # Smart routing (Issue #1317): auto-attach if providers available
        self._router: Any | None = None
        if self.providers:
            try:
                from nexus.sandbox.sandbox_router import SandboxRouter

                self._router = SandboxRouter(available_providers=self.providers)
            except Exception as e:
                logger.warning(f"Failed to initialize sandbox router: {e}")

        logger.info(f"Initialized sandbox manager with providers: {list(self.providers.keys())}")

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        """Create a fresh session for a single DB operation."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _execute_with_retry(self, operation: Callable[[Session], _T], context: str = "query") -> _T:
        """Execute a database operation with one retry on PendingRollbackError.

        Each attempt gets a fresh session from the factory.

        Args:
            operation: Callable that accepts a Session and returns a value.
            context: Human-readable label for log messages.

        Returns:
            Whatever ``operation`` returns.

        Raises:
            SQLAlchemyError: If the retry also fails.
        """
        try:
            with self._get_session() as session:
                return operation(session)
        except (PendingRollbackError, SQLAlchemyError) as exc:
            logger.warning("Database error during %s: %s", context, exc)
            try:
                with self._get_session() as session:
                    return operation(session)
            except SQLAlchemyError as retry_exc:
                logger.error("Database error persisted after retry: %s", retry_exc)
                raise

    def _get_metadata(self, sandbox_id: str) -> dict[str, Any]:
        """Get sandbox metadata from database as a dict.

        Returns a dict to avoid detached ORM object issues across sessions.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox metadata dict with all fields

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """

        def _query(session: Session) -> SandboxMetadataModel | None:
            result = session.execute(
                select(SandboxMetadataModel).where(SandboxMetadataModel.sandbox_id == sandbox_id)
            )
            return result.scalar_one_or_none()

        metadata = self._execute_with_retry(_query, context="metadata lookup")

        if not metadata:
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")

        return self._metadata_to_dict(metadata)

    def _get_metadata_field(self, sandbox_id: str, field: str) -> Any:
        """Get a single field from sandbox metadata."""

        def _query(session: Session) -> Any:
            result = session.execute(
                select(SandboxMetadataModel).where(SandboxMetadataModel.sandbox_id == sandbox_id)
            )
            metadata = result.scalar_one_or_none()
            if not metadata:
                raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")
            return getattr(metadata, field)

        return self._execute_with_retry(_query, context=f"metadata field {field}")

    def _update_metadata(self, sandbox_id: str, **updates: Any) -> dict[str, Any]:
        """Re-query and update metadata fields in a fresh session.

        Args:
            sandbox_id: Sandbox ID
            **updates: Field name -> value pairs to update

        Returns:
            Updated metadata dict

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        with self._get_session() as session:
            metadata = session.execute(
                select(SandboxMetadataModel).where(SandboxMetadataModel.sandbox_id == sandbox_id)
            ).scalar_one_or_none()
            if not metadata:
                raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")
            for key, value in updates.items():
                setattr(metadata, key, value)
            session.flush()
            session.refresh(metadata)
            return self._metadata_to_dict(metadata)

    async def create_sandbox(
        self,
        name: str,
        user_id: str,
        zone_id: str,
        agent_id: str | None = None,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new sandbox.

        Args:
            name: User-friendly name (unique per user)
            user_id: User ID
            zone_id: Zone ID
            agent_id: Agent ID (optional)
            ttl_minutes: Idle timeout in minutes
            provider: Provider name ("docker", "e2b", etc.). If None, selects best available.
            template_id: Template ID for provider

        Returns:
            Sandbox metadata dict with sandbox_id, name, status, etc.

        Raises:
            ValueError: If provider not available or name already exists
            SandboxCreationError: If sandbox creation fails
        """
        # Auto-select provider if not specified (prefer docker -> e2b)
        if provider is None:
            if "docker" in self.providers:
                provider = "docker"
            elif "e2b" in self.providers:
                provider = "e2b"
            else:
                available = ", ".join(self.providers.keys()) if self.providers else "none"
                raise ValueError(
                    f"No sandbox providers available. Available providers: {available}"
                )

        # Check provider availability
        if provider not in self.providers:
            available = ", ".join(self.providers.keys()) if self.providers else "none"
            raise ValueError(
                f"Provider '{provider}' not available. Available providers: {available}"
            )

        # Check name uniqueness for active sandboxes only
        def _check_name(session: Session) -> SandboxMetadataModel | None:
            result = session.execute(
                select(SandboxMetadataModel).where(
                    SandboxMetadataModel.user_id == user_id,
                    SandboxMetadataModel.name == name,
                    SandboxMetadataModel.status == "active",
                )
            )
            return result.scalar_one_or_none()

        if self._execute_with_retry(_check_name, context="name uniqueness check"):
            raise ValueError(
                f"Active sandbox with name '{name}' already exists for user {user_id}. "
                f"Use sandbox_get_or_create() to reuse it or choose a different name."
            )

        # Resolve agent trust tier to security profile
        from nexus.sandbox.security_profile import SandboxSecurityProfile

        agent_name = agent_id.split(",", 1)[1] if agent_id and "," in agent_id else None
        security_profile = SandboxSecurityProfile.from_trust_tier(agent_name)
        logger.info(
            f"Using '{security_profile.name}' security profile for sandbox (agent={agent_id})"
        )

        # Create sandbox via provider (async call)
        provider_obj = self.providers[provider]
        sandbox_id = await provider_obj.create(
            template_id=template_id,
            timeout_minutes=ttl_minutes,
            metadata={"name": name},
            security_profile=security_profile,
        )

        # OPTIMIZATION: Start pre-warming Python imports in background
        if hasattr(provider_obj, "prewarm_imports"):
            try:
                await provider_obj.prewarm_imports(sandbox_id)
            except Exception as e:
                logger.debug(f"Pre-warm failed (non-fatal): {e}")

        # Calculate expiry time
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=ttl_minutes)

        # Create database record in a fresh session
        try:
            with self._get_session() as session:
                metadata = SandboxMetadataModel(
                    sandbox_id=sandbox_id,
                    name=name,
                    user_id=user_id,
                    agent_id=agent_id,
                    zone_id=zone_id,
                    provider=provider,
                    template_id=template_id,
                    status="active",
                    created_at=now,
                    last_active_at=now,
                    ttl_minutes=ttl_minutes,
                    expires_at=expires_at,
                    auto_created=1,  # PostgreSQL integer type
                )
                session.add(metadata)
                session.flush()
                session.refresh(metadata)
                result_dict = self._metadata_to_dict(metadata)
        except SQLAlchemyError as e:
            logger.error(f"Failed to commit new sandbox metadata: {e}")
            # Cleanup orphaned container to prevent resource leak (#1307)
            try:
                await provider_obj.destroy(sandbox_id)
                logger.info(f"Cleaned up orphaned container {sandbox_id} after DB failure")
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup orphaned container {sandbox_id}: {cleanup_err}")
            raise

        logger.info(
            f"Created sandbox {sandbox_id} (name={name}, user={user_id}, provider={provider})"
        )

        return result_dict

    def set_monty_host_functions(
        self,
        sandbox_id: str,
        host_functions: dict[str, Callable[..., Any]],
    ) -> None:
        """Set host function callbacks for a Monty sandbox (Issue #1316).

        Host functions bridge Monty's isolated interpreter to Nexus VFS
        and services. They are called when sandboxed code invokes external
        functions. The caller is responsible for constructing agent-scoped
        callables with appropriate permission checks.

        This is a no-op if the sandbox is not a Monty sandbox.

        Args:
            sandbox_id: Sandbox ID.
            host_functions: Mapping of function name to callable.
                Callables must validate all inputs (sandboxed code
                controls the arguments).

        Example:
            host_fns = {
                "read_file": lambda path: nexus.read(path, agent_id=aid),
                "write_file": lambda path, content: nexus.write(path, content, agent_id=aid),
            }
            manager.set_monty_host_functions(sandbox_id, host_fns)
        """
        monty_provider = self.providers.get("monty")
        if monty_provider is None:
            logger.debug("Monty provider not available, skipping host function setup")
            return

        # Narrow type for mypy — only MontySandboxProvider has set_host_functions
        if not MONTY_PROVIDER_AVAILABLE or not isinstance(monty_provider, MontySandboxProvider):
            return

        try:
            monty_provider.set_host_functions(sandbox_id, host_functions)
        except SandboxNotFoundError:
            # Non-fatal — sandbox may not be a Monty sandbox
            logger.debug(
                "Failed to set host functions for sandbox %s (not a Monty sandbox)",
                sandbox_id,
            )

        # Cache in router for re-wiring on escalation (Issue #1317)
        router = getattr(self, "_router", None)
        if router is not None:
            # Look up agent_id from sandbox metadata
            try:
                meta = self._get_metadata(sandbox_id)
                agent_id = meta.get("agent_id")
                if agent_id:
                    router.cache_host_functions(agent_id, host_functions)
            except SandboxNotFoundError:
                pass

    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        as_script: bool = False,
        auto_validate: bool | None = None,  # noqa: ARG002
    ) -> CodeExecutionResult:
        """Run code in sandbox.

        When a router is configured (Issue #1317), handles EscalationNeeded
        exceptions by retrying on the next tier in the escalation chain
        (monty -> docker -> e2b).

        Args:
            sandbox_id: Sandbox ID
            language: Programming language
            code: Code to execute
            timeout: Timeout in seconds
            as_script: If True, run as standalone script (stateless).
                      If False (default), use Jupyter kernel for Python (stateful).
            auto_validate: If True, run validation after execution.
                If None, use pipeline config's auto_run setting.
                If False, skip validation.

        Returns:
            CodeExecutionResult with stdout, stderr, exit_code, execution_time,
            and optional validations list.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        # Get metadata (provider name and TTL)
        meta_dict = self._get_metadata(sandbox_id)
        provider_name = meta_dict["provider"]
        ttl = meta_dict["ttl_minutes"]
        agent_id = meta_dict.get("agent_id")

        # Run code via provider — with escalation support
        provider = self.providers[provider_name]
        try:
            result = await provider.run_code(
                sandbox_id, language, code, timeout, as_script=as_script
            )
        except EscalationNeeded as exc:
            result = await self._handle_escalation(
                exc, provider_name, language, code, timeout, as_script, agent_id
            )

        # Record execution in router if available
        router = getattr(self, "_router", None)
        if router is not None and agent_id:
            router.record_execution(agent_id, provider_name, escalated=False)

        # Update last_active_at and expires_at
        now = datetime.now(UTC)
        self._update_metadata(
            sandbox_id,
            last_active_at=now,
            expires_at=now + timedelta(minutes=ttl),
        )

        logger.debug(f"Executed {language} code in sandbox {sandbox_id}")

        return result

    async def _handle_escalation(
        self,
        exc: EscalationNeeded,
        from_tier: str,
        language: str,
        code: str,
        timeout: int,
        as_script: bool,
        agent_id: str | None,
    ) -> CodeExecutionResult:
        """Handle EscalationNeeded by retrying on the next tier.

        Args:
            exc: The escalation exception.
            from_tier: Tier that raised EscalationNeeded.
            language: Programming language.
            code: Code to execute.
            timeout: Execution timeout.
            as_script: Script mode flag.
            agent_id: Agent ID for history tracking.

        Returns:
            Result from the escalated provider.

        Raises:
            EscalationNeeded: If no next tier is available.
        """
        router = getattr(self, "_router", None)
        if router is None:
            raise exc

        # Determine next tier
        to_tier = exc.suggested_tier
        if to_tier and to_tier in self.providers:
            next_tier = to_tier
        else:
            next_tier = router.get_next_tier(from_tier)

        if next_tier is None:
            logger.warning(
                "Escalation from %s failed: no next tier available (reason: %s)",
                from_tier,
                exc.reason,
            )
            raise exc

        logger.info(
            "Escalating from %s to %s (reason: %s)",
            from_tier,
            next_tier,
            exc.reason,
        )

        # Record escalation
        if agent_id:
            router.record_escalation(agent_id, from_tier, next_tier)

        # Create a temporary sandbox on the next tier and run code
        next_provider = self.providers[next_tier]
        temp_sandbox_id = await next_provider.create(timeout_minutes=5)

        try:
            # Re-wire host functions if escalating from monty
            if from_tier == "monty" and agent_id and router:
                cached_fns = router.get_cached_host_functions(agent_id)
                if cached_fns and hasattr(next_provider, "set_host_functions"):
                    next_provider.set_host_functions(temp_sandbox_id, cached_fns)

            result = await next_provider.run_code(
                temp_sandbox_id, language, code, timeout, as_script=as_script
            )
        finally:
            # Clean up temporary sandbox
            try:
                await next_provider.destroy(temp_sandbox_id)
            except Exception as cleanup_err:
                logger.warning(
                    "Failed to destroy temp sandbox %s: %s",
                    temp_sandbox_id,
                    cleanup_err,
                )

        return result

    async def validate(
        self,
        sandbox_id: str,
        workspace_path: str = "/workspace",
    ) -> list[dict[str, Any]]:
        """Run validation pipeline in sandbox.

        Explicit validation API — detects project type and runs applicable
        linters, returning structured results.

        Args:
            sandbox_id: Sandbox ID
            workspace_path: Path to workspace root in sandbox.

        Returns:
            List of validation result dicts.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        from nexus.validation import ValidationRunner

        meta_dict = self._get_metadata(sandbox_id)
        provider_name = meta_dict["provider"]
        provider = self.providers[provider_name]

        runner = ValidationRunner()
        results = await runner.validate(sandbox_id, provider, workspace_path)
        return [r.model_dump() for r in results]

    async def pause_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Pause sandbox.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Updated sandbox metadata

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            UnsupportedOperationError: If provider doesn't support pause
        """
        meta_dict = self._get_metadata(sandbox_id)
        provider_name = meta_dict["provider"]

        # Pause via provider
        provider = self.providers[provider_name]
        await provider.pause(sandbox_id)

        # Update metadata
        result = self._update_metadata(
            sandbox_id,
            status="paused",
            paused_at=datetime.now(UTC),
            expires_at=None,
        )

        logger.info(f"Paused sandbox {sandbox_id}")
        return result

    async def resume_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Resume paused sandbox.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Updated sandbox metadata

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            UnsupportedOperationError: If provider doesn't support resume
        """
        meta_dict = self._get_metadata(sandbox_id)
        provider_name = meta_dict["provider"]
        ttl = meta_dict["ttl_minutes"]

        # Resume via provider
        provider = self.providers[provider_name]
        await provider.resume(sandbox_id)

        # Update metadata
        now = datetime.now(UTC)
        result = self._update_metadata(
            sandbox_id,
            status="active",
            last_active_at=now,
            expires_at=now + timedelta(minutes=ttl),
            paused_at=None,
        )

        logger.info(f"Resumed sandbox {sandbox_id}")
        return result

    async def stop_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Stop and destroy sandbox.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Updated sandbox metadata

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        meta_dict = self._get_metadata(sandbox_id)
        provider_name = meta_dict["provider"]

        # Destroy via provider
        provider = self.providers[provider_name]
        await provider.destroy(sandbox_id)

        # Update metadata
        result = self._update_metadata(
            sandbox_id,
            status="stopped",
            stopped_at=datetime.now(UTC),
            expires_at=None,
        )

        logger.info(f"Stopped sandbox {sandbox_id}")
        return result

    async def list_sandboxes(
        self,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        verify_status: bool = False,
    ) -> list[dict[str, Any]]:
        """List sandboxes with optional filtering.

        Args:
            user_id: Filter by user (optional)
            zone_id: Filter by zone (optional)
            agent_id: Filter by agent (optional)
            status: Filter by status (e.g., 'active', 'stopped', 'paused') (optional)
            verify_status: If True, verify status with provider (slower but accurate)

        Returns:
            List of sandbox metadata dicts
        """
        query = select(SandboxMetadataModel)

        if user_id:
            query = query.where(SandboxMetadataModel.user_id == user_id)
        if zone_id:
            query = query.where(SandboxMetadataModel.zone_id == zone_id)
        if agent_id:
            query = query.where(SandboxMetadataModel.agent_id == agent_id)
        if status:
            query = query.where(SandboxMetadataModel.status == status)

        def _list_query(session: Session) -> list[dict[str, Any]]:
            sandboxes = list(session.execute(query).scalars().all())
            return [
                {
                    **self._metadata_to_dict(sb),
                    "_sandbox_id": sb.sandbox_id,
                    "_provider": sb.provider,
                    "_status": sb.status,
                }
                for sb in sandboxes
            ]

        sandbox_dicts = self._execute_with_retry(_list_query, context="sandbox list")

        # Strip internal fields and optionally verify status
        result_dicts = []
        for sb_dict in sandbox_dicts:
            sb_id = sb_dict.pop("_sandbox_id")
            sb_provider = sb_dict.pop("_provider")
            sb_status = sb_dict.pop("_status")
            result_dicts.append(sb_dict)

            if not verify_status:
                continue

            try:
                provider = self.providers.get(sb_provider)
                if not provider:
                    logger.warning(f"Provider '{sb_provider}' not available for sandbox {sb_id}")
                    sb_dict["verified"] = False
                    continue

                provider_info = await provider.get_info(sb_id)
                actual_status = provider_info.status

                sb_dict["verified"] = True
                sb_dict["provider_status"] = actual_status

                if actual_status != sb_status:
                    logger.info(
                        f"Status mismatch for {sb_id}: "
                        f"DB={sb_status}, Provider={actual_status}. Updating DB."
                    )
                    updates: dict[str, Any] = {"status": actual_status}
                    if actual_status == "stopped":
                        updates["stopped_at"] = datetime.now(UTC)
                        updates["expires_at"] = None
                    self._update_metadata(sb_id, **updates)
                    sb_dict["status"] = actual_status

            except SandboxNotFoundError:
                logger.warning(f"Sandbox {sb_id} not found in provider. Marking as stopped.")
                sb_dict["verified"] = True
                sb_dict["provider_status"] = "stopped"

                if sb_status != "stopped":
                    self._update_metadata(
                        sb_id,
                        status="stopped",
                        stopped_at=datetime.now(UTC),
                        expires_at=None,
                    )
                    sb_dict["status"] = "stopped"

            except Exception as e:
                logger.warning(f"Failed to verify status for {sb_id}: {e}")
                sb_dict["verified"] = False

        return result_dicts

    async def get_sandbox_status(self, sandbox_id: str) -> dict[str, Any]:
        """Get sandbox status and metadata.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox metadata dict

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        return self._get_metadata(sandbox_id)

    async def get_or_create_sandbox(
        self,
        name: str,
        user_id: str,
        zone_id: str,
        agent_id: str | None = None,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
    ) -> dict[str, Any]:
        """Get existing active sandbox or create a new one.

        OPTIMIZED: Queries DB directly for exact name match instead of listing all.
        This reduces get_or_create from ~20s to ~2s by avoiding verification of
        unrelated sandboxes.

        Args:
            name: User-friendly sandbox name (unique per user)
            user_id: User ID
            zone_id: Zone ID
            agent_id: Agent ID (optional)
            ttl_minutes: Idle timeout in minutes (default: 10)
            provider: Sandbox provider ("docker", "e2b", etc.)
            template_id: Provider template ID (optional)
            verify_status: If True, verify status with provider (default: True)

        Returns:
            Sandbox metadata dict (either existing or newly created)

        Raises:
            ValueError: If provider not available
            SandboxCreationError: If sandbox creation fails
        """

        # OPTIMIZATION: Query directly for exact name match instead of listing all
        def _find_active(session: Session) -> dict[str, Any] | None:
            result = session.execute(
                select(SandboxMetadataModel).where(
                    SandboxMetadataModel.user_id == user_id,
                    SandboxMetadataModel.name == name,
                    SandboxMetadataModel.status == "active",
                )
            )
            metadata = result.scalar_one_or_none()
            if metadata:
                return {
                    **self._metadata_to_dict(metadata),
                    "_sandbox_id": metadata.sandbox_id,
                    "_provider": metadata.provider,
                }
            return None

        existing = self._execute_with_retry(_find_active, context="sandbox lookup")

        if existing:
            sb_id = existing.pop("_sandbox_id")
            sb_provider = existing.pop("_provider")

            if verify_status:
                try:
                    provider_obj = self.providers.get(sb_provider)
                    if provider_obj:
                        provider_info = await provider_obj.get_info(sb_id)
                        actual_status = provider_info.status

                        if actual_status == "active":
                            existing["verified"] = True
                            existing["provider_status"] = actual_status
                            logger.info(
                                f"Found and verified existing sandbox {sb_id} "
                                f"(name={name}, user={user_id})"
                            )
                            return existing
                        else:
                            logger.warning(
                                f"Sandbox {sb_id} status mismatch: "
                                f"DB=active, Provider={actual_status}. Creating new."
                            )
                            self._update_metadata(
                                sb_id,
                                status="stopped",
                                stopped_at=datetime.now(UTC),
                                expires_at=None,
                            )
                    else:
                        logger.warning(f"Provider '{sb_provider}' not available for verification")
                except SandboxNotFoundError:
                    logger.warning(
                        f"Sandbox {sb_id} not found in provider. "
                        f"Marking as stopped and creating new."
                    )
                    self._update_metadata(
                        sb_id,
                        status="stopped",
                        stopped_at=datetime.now(UTC),
                        expires_at=None,
                    )
                except Exception as e:
                    logger.warning(f"Failed to verify sandbox {sb_id}: {e}")
            else:
                logger.info(
                    f"Found existing sandbox {sb_id} (name={name}, user={user_id}) - not verified"
                )
                return existing

        # No active sandbox found - create new one
        logger.info(
            f"No active sandbox found for name={name}, user={user_id}. Creating new sandbox..."
        )

        try:
            return await self.create_sandbox(
                name=name,
                user_id=user_id,
                zone_id=zone_id,
                agent_id=agent_id,
                ttl_minutes=ttl_minutes,
                provider=provider,
                template_id=template_id,
            )
        except ValueError as e:
            if "already exists" in str(e):
                # Race condition: sandbox was created between check and create
                logger.warning("Sandbox name conflict detected. Cleaning up stale sandbox...")
                try:

                    def _cleanup_stale(session: Session) -> None:
                        result = session.execute(
                            select(SandboxMetadataModel).where(
                                SandboxMetadataModel.user_id == user_id,
                                SandboxMetadataModel.name == name,
                            )
                        )
                        stale = result.scalar_one_or_none()
                        if stale:
                            stale.status = "stopped"
                            stale.stopped_at = datetime.now(UTC)
                            logger.info(f"Marked stale sandbox {stale.sandbox_id} as stopped")

                    self._execute_with_retry(_cleanup_stale, context="stale sandbox cleanup")
                except SQLAlchemyError as db_err:
                    logger.error(f"Database error during stale sandbox cleanup: {db_err}")
                    raise

                # Retry create with modified name
                new_name = f"{name}-{datetime.now(UTC).strftime('%H%M%S')}"
                logger.info(f"Retrying with name: {new_name}")
                return await self.create_sandbox(
                    name=new_name,
                    user_id=user_id,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    ttl_minutes=ttl_minutes,
                    provider=provider,
                    template_id=template_id,
                )
            else:
                raise

    async def connect_sandbox(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,  # noqa: ARG002 - Reserved for user-managed sandboxes
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        skip_dependency_checks: bool | None = None,
    ) -> dict[str, Any]:
        """Connect and mount Nexus to a sandbox (Nexus-managed or user-managed).

        Works for both:
        - Nexus-managed sandboxes (created via sandbox_create) - no sandbox_api_key needed
        - User-managed sandboxes (external) - requires sandbox_api_key

        Args:
            sandbox_id: Sandbox ID (Nexus-managed or external)
            provider: Provider name ("e2b", "docker", etc.)
            sandbox_api_key: Provider API key (optional, only for user-managed sandboxes)
            mount_path: Path where Nexus will be mounted in sandbox
            nexus_url: Nexus server URL (required for mounting)
            nexus_api_key: Nexus API key (required for mounting)
            agent_id: Optional agent ID for version attribution (issue #418).
                When set, file modifications will be attributed to this agent.
            skip_dependency_checks: If True, skip nexus/fusepy installation checks.
                If None (default), auto-detect based on template (skip for known templates
                like nexus-sandbox that have dependencies pre-installed).

        Returns:
            Dict with connection details (sandbox_id, provider, mount_path, mounted_at, mount_status)

        Raises:
            ValueError: If provider not available or required credentials missing
            RuntimeError: If connection/mount fails
        """
        # Check provider availability
        if provider not in self.providers:
            available = ", ".join(self.providers.keys()) if self.providers else "none"
            raise ValueError(
                f"Provider '{provider}' not available. Available providers: {available}"
            )

        if not nexus_url or not nexus_api_key:
            raise ValueError("Both nexus_url and nexus_api_key required for mounting")

        # Get provider
        provider_obj = self.providers[provider]

        # OPTIMIZATION: Auto-detect skip_dependency_checks based on template
        if skip_dependency_checks is None:
            try:
                meta_dict = self._get_metadata(sandbox_id)
                tid = meta_dict.get("template_id")
                preinstalled_templates = {"nexus-sandbox", "nexus-fuse", "aquarius-worker"}
                if tid and any(t in tid for t in preinstalled_templates):
                    skip_dependency_checks = True
                    logger.info(f"Auto-skipping dependency checks for template '{tid}'")
                else:
                    skip_dependency_checks = False
            except SandboxNotFoundError:
                skip_dependency_checks = False

        logger.info(
            f"Connecting to sandbox {sandbox_id} (provider={provider}, mount={mount_path}, "
            f"skip_checks={skip_dependency_checks})"
        )

        mount_result = await provider_obj.mount_nexus(
            sandbox_id=sandbox_id,
            mount_path=mount_path,
            nexus_url=nexus_url,
            api_key=nexus_api_key,
            agent_id=agent_id,
            skip_dependency_checks=skip_dependency_checks,
        )

        now = datetime.now(UTC)

        if mount_result["success"]:
            logger.info(f"Successfully mounted Nexus in sandbox {sandbox_id} at {mount_path}")
        else:
            logger.warning(
                f"Failed to mount Nexus in sandbox {sandbox_id}: {mount_result['message']}"
            )

        return {
            "success": mount_result["success"],
            "sandbox_id": sandbox_id,
            "provider": provider,
            "mount_path": mount_path,
            "mounted_at": now.isoformat(),
            "mount_status": mount_result,
        }

    async def disconnect_sandbox(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
    ) -> dict[str, Any]:
        """Disconnect and unmount Nexus from a user-managed sandbox.

        Args:
            sandbox_id: External sandbox ID
            provider: Provider name ("e2b", "docker", etc.)
            sandbox_api_key: Provider API key for authentication

        Returns:
            Dict with disconnection details (sandbox_id, provider, unmounted_at)

        Raises:
            ValueError: If provider not available or API key missing
            RuntimeError: If disconnection/unmount fails
        """
        # Check provider availability
        if provider not in self.providers:
            available = ", ".join(self.providers.keys()) if self.providers else "none"
            raise ValueError(
                f"Provider '{provider}' not available. Available providers: {available}"
            )

        if not sandbox_api_key:
            raise ValueError(f"Sandbox API key required for provider '{provider}'")

        # Get provider
        _ = self.providers[provider]

        logger.info(f"Disconnecting from user-managed sandbox {sandbox_id} (provider={provider})")

        # Execute unmount command remotely in sandbox
        # TODO: Implement actual unmount execution via provider

        now = datetime.now(UTC)

        logger.info(f"Disconnected from sandbox {sandbox_id}")

        return {
            "success": True,
            "sandbox_id": sandbox_id,
            "provider": provider,
            "unmounted_at": now.isoformat(),
        }

    async def cleanup_expired_sandboxes(self) -> int:
        """Clean up expired sandboxes.

        Returns:
            Number of sandboxes cleaned up
        """
        now = datetime.now(UTC)

        def _find_expired(session: Session) -> list[str]:
            result = session.execute(
                select(SandboxMetadataModel.sandbox_id).where(
                    SandboxMetadataModel.status == "active",
                    SandboxMetadataModel.expires_at < now,
                )
            )
            return list(result.scalars().all())

        try:
            expired_ids = self._execute_with_retry(_find_expired, context="expired sandbox query")
        except SQLAlchemyError:
            return 0

        count = 0
        for sb_id in expired_ids:
            try:
                await self.stop_sandbox(sb_id)
                count += 1
            except Exception as e:
                logger.error(f"Failed to cleanup sandbox {sb_id}: {e}")

        if count > 0:
            logger.info(f"Cleaned up {count} expired sandboxes")

        return count

    def _metadata_to_dict(self, metadata: SandboxMetadataModel) -> dict[str, Any]:
        """Convert metadata model to dict.

        Must be called while the session that loaded the model is still open.

        Args:
            metadata: Sandbox metadata model

        Returns:
            Metadata dict
        """
        # Ensure created_at is timezone-aware for uptime calculation
        created_at = metadata.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        return {
            "sandbox_id": metadata.sandbox_id,
            "name": metadata.name,
            "user_id": metadata.user_id,
            "agent_id": metadata.agent_id,
            "zone_id": metadata.zone_id,
            "provider": metadata.provider,
            "template_id": metadata.template_id,
            "status": metadata.status,
            "created_at": metadata.created_at.isoformat(),
            "last_active_at": metadata.last_active_at.isoformat(),
            "paused_at": metadata.paused_at.isoformat() if metadata.paused_at else None,
            "stopped_at": metadata.stopped_at.isoformat() if metadata.stopped_at else None,
            "ttl_minutes": metadata.ttl_minutes,
            "expires_at": metadata.expires_at.isoformat() if metadata.expires_at else None,
            "uptime_seconds": (datetime.now(UTC) - created_at).total_seconds(),
        }

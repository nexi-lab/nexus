"""Sandbox manager for Nexus-managed sandboxes.

Thin orchestrator that coordinates sandbox lifecycle management using
ProviderRegistry (provider discovery/selection) and SandboxRepository
(database persistence). Handles creation, TTL tracking, and cleanup.

Issue #2051: Decomposed from 1,210-line god object into focused services.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.sandbox.sandbox_router import SandboxRouter

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from nexus.bricks.sandbox.provider_registry import ProviderRegistry
from nexus.bricks.sandbox.repository import SandboxRepository
from nexus.bricks.sandbox.sandbox_provider import (
    CodeExecutionResult,
    EscalationNeeded,
    SandboxNotFoundError,
    SandboxProvider,
)

logger = logging.getLogger(__name__)


class _ProvidersDictProxy(dict):
    """Dict proxy that writes through to ProviderRegistry on __setitem__.

    Returned by ``SandboxManager.providers`` so callers that do
    ``mgr.providers["docker"] = provider`` actually register in the registry.
    Read operations return a snapshot; writes propagate immediately.
    """

    def __init__(self, registry: ProviderRegistry, data: dict[str, SandboxProvider]) -> None:
        super().__init__(data)
        self._registry = registry

    def __setitem__(self, name: str, provider: SandboxProvider) -> None:
        super().__setitem__(name, provider)
        self._registry.register(name, provider)

    def __delitem__(self, name: str) -> None:
        super().__delitem__(name)
        # Remove from registry internals
        self._registry._providers.pop(name, None)
        self._registry._factories.pop(name, None)


class SandboxManager:
    """Manages sandboxes across different providers with database persistence.

    Thin orchestrator that delegates:
    - Database operations → SandboxRepository
    - Provider discovery/selection → ProviderRegistry
    - Docker mount pipeline → DockerMountService (via provider)

    Note: Providers are async. Database operations use sync sessions (session-per-op).
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        e2b_api_key: str | None = None,
        e2b_team_id: str | None = None,
        e2b_template_id: str | None = None,
        config: Any = None,  # NexusConfig | None
        *,
        repository: SandboxRepository | None = None,
        registry: ProviderRegistry | None = None,
    ):
        """Initialize sandbox manager.

        Args:
            session_factory: Factory that creates fresh DB sessions.
            e2b_api_key: E2B API key.
            e2b_team_id: E2B team ID.
            e2b_template_id: Default E2B template ID.
            config: Nexus configuration (for Docker templates).
            repository: Optional pre-built repository (for testing).
            registry: Optional pre-built registry (for testing).
        """
        self._repository = repository or SandboxRepository(session_factory)
        self._registry = registry or self._build_default_registry(
            e2b_api_key, e2b_team_id, e2b_template_id, config
        )

        # Smart routing (Issue #1317)
        self._router: SandboxRouter | None = None

        logger.info(
            "Initialized sandbox manager with providers: %s",
            self._registry.available_names(),
        )

    @property
    def providers(self) -> dict[str, SandboxProvider]:
        """Backward-compatible mutable access to initialized providers.

        Returns a dict proxy that writes through to the registry on assignment.
        This allows ``mgr.providers["docker"] = provider`` to work as expected.
        Read operations return a snapshot of eagerly-initialized providers.
        """
        return _ProvidersDictProxy(self._registry, dict(self._registry.items()))

    def wire_router(self) -> None:
        """Attach a SandboxRouter for smart tier selection (Issue #1317).

        Creates and attaches a router if providers are available.
        Idempotent — safe to call multiple times.
        """
        if self._registry.is_empty():
            return

        from nexus.bricks.sandbox.sandbox_router import SandboxRouter

        self._router = SandboxRouter(available_providers=self.providers)
        logger.info(
            "SandboxRouter wired with providers: %s",
            self._registry.available_names(),
        )

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
            name: User-friendly name (unique per user).
            user_id: User ID.
            zone_id: Zone ID.
            agent_id: Agent ID (optional).
            ttl_minutes: Idle timeout in minutes.
            provider: Provider name. If None, selects best available.
            template_id: Template ID for provider.

        Returns:
            Sandbox metadata dict.

        Raises:
            ValueError: If provider not available or name already exists.
            SandboxCreationError: If sandbox creation fails.
        """
        # Auto-select provider
        if provider is None:
            provider = self._registry.auto_select()

        # Validate provider availability (raises ValueError if missing)
        provider_obj = self._registry.get(provider)

        # Check name uniqueness for active sandboxes
        if self._repository.find_active_by_name(user_id, name) is not None:
            raise ValueError(
                f"Active sandbox with name '{name}' already exists for user {user_id}. "
                f"Use sandbox_get_or_create() to reuse it or choose a different name."
            )

        # Resolve agent trust tier to security profile
        from nexus.bricks.sandbox.security_profile import SandboxSecurityProfile

        agent_name = agent_id.split(",", 1)[1] if agent_id and "," in agent_id else None
        security_profile = SandboxSecurityProfile.from_trust_tier(agent_name)
        logger.info(
            "Using '%s' security profile for sandbox (agent=%s)",
            security_profile.name,
            agent_id,
        )

        # Create sandbox via provider
        sandbox_id = await provider_obj.create(
            template_id=template_id,
            timeout_minutes=ttl_minutes,
            metadata={"name": name},
            security_profile=security_profile,
        )

        # Pre-warm Python imports in background
        if hasattr(provider_obj, "prewarm_imports"):
            try:
                await provider_obj.prewarm_imports(sandbox_id)
            except Exception as e:
                logger.debug("Pre-warm failed (non-fatal): %s", e)

        # Create database record
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=ttl_minutes)

        try:
            result_dict = self._repository.create_metadata(
                sandbox_id=sandbox_id,
                name=name,
                user_id=user_id,
                zone_id=zone_id,
                agent_id=agent_id,
                provider=provider,
                template_id=template_id,
                created_at=now,
                last_active_at=now,
                ttl_minutes=ttl_minutes,
                expires_at=expires_at,
            )
        except SQLAlchemyError as e:
            logger.error("Failed to commit new sandbox metadata: %s", e)
            # Cleanup orphaned container to prevent resource leak (#1307)
            try:
                await provider_obj.destroy(sandbox_id)
                logger.info("Cleaned up orphaned container %s after DB failure", sandbox_id)
            except Exception as cleanup_err:
                logger.warning(
                    "Failed to cleanup orphaned container %s: %s", sandbox_id, cleanup_err
                )
            raise

        logger.info(
            "Created sandbox %s (name=%s, user=%s, provider=%s)",
            sandbox_id,
            name,
            user_id,
            provider,
        )
        return result_dict

    def set_monty_host_functions(
        self,
        sandbox_id: str,
        host_functions: dict[str, Callable[..., Any]],
    ) -> None:
        """Set host function callbacks for a Monty sandbox (Issue #1316).

        This is a no-op if the sandbox is not a Monty sandbox.

        Args:
            sandbox_id: Sandbox ID.
            host_functions: Mapping of function name to callable.
        """
        if not self._registry.has("monty"):
            logger.debug("Monty provider not available, skipping host function setup")
            return

        monty_provider = self._registry.get("monty")

        if not hasattr(monty_provider, "set_host_functions"):
            return

        try:
            monty_provider.set_host_functions(sandbox_id, host_functions)
        except SandboxNotFoundError:
            logger.debug(
                "Failed to set host functions for sandbox %s (not a Monty sandbox)",
                sandbox_id,
            )

        # Cache in router for re-wiring on escalation (Issue #1317)
        if self._router is not None:
            try:
                meta = self._repository.get_metadata(sandbox_id)
                agent_id = meta.get("agent_id")
                if agent_id:
                    self._router.cache_host_functions(agent_id, host_functions)
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

        Handles EscalationNeeded by retrying on the next tier when a router
        is configured.

        Args:
            sandbox_id: Sandbox ID.
            language: Programming language.
            code: Code to execute.
            timeout: Timeout in seconds.
            as_script: If True, run as standalone script.
            auto_validate: Reserved for validation pipeline.

        Returns:
            CodeExecutionResult.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """
        meta_dict = self._repository.get_metadata(sandbox_id)
        provider_name = meta_dict["provider"]
        ttl = meta_dict["ttl_minutes"]
        agent_id = meta_dict.get("agent_id")

        provider = self._registry.get(provider_name)
        escalated = False
        try:
            result = await provider.run_code(
                sandbox_id, language, code, timeout, as_script=as_script
            )
        except EscalationNeeded as exc:
            result = await self._handle_escalation(
                exc, provider_name, language, code, timeout, as_script, agent_id
            )
            escalated = True

        # Record execution in router if available
        if self._router is not None and agent_id:
            self._router.record_execution(agent_id, provider_name, escalated=escalated)

        # Update last_active_at and expires_at
        now = datetime.now(UTC)
        self._repository.update_metadata(
            sandbox_id,
            last_active_at=now,
            expires_at=now + timedelta(minutes=ttl),
        )

        logger.debug("Executed %s code in sandbox %s", language, sandbox_id)
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
        if self._router is None:
            raise exc

        # Determine next tier
        next_tier: str | None = (
            exc.suggested_tier
            if exc.suggested_tier and self._registry.has(exc.suggested_tier)
            else None
        )
        if next_tier is None:
            next_tier = self._router.get_next_tier(from_tier)

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
            self._router.record_escalation(agent_id, from_tier, next_tier)

        # Create a temporary sandbox on the next tier and run code
        next_provider = self._registry.get(next_tier)
        temp_sandbox_id = await next_provider.create(timeout_minutes=5)

        try:
            # Re-wire host functions if escalating from monty
            if from_tier == "monty" and agent_id:
                cached_fns = self._router.get_cached_host_functions(agent_id)
                if cached_fns and hasattr(next_provider, "set_host_functions"):
                    next_provider.set_host_functions(temp_sandbox_id, cached_fns)

            result = await next_provider.run_code(
                temp_sandbox_id, language, code, timeout, as_script=as_script
            )
        finally:
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

        Args:
            sandbox_id: Sandbox ID.
            workspace_path: Path to workspace root in sandbox.

        Returns:
            List of validation result dicts.
        """
        from nexus.validation import ValidationRunner

        meta_dict = self._repository.get_metadata(sandbox_id)
        provider_name = meta_dict["provider"]
        provider = self._registry.get(provider_name)

        runner = ValidationRunner()
        results = await runner.validate(sandbox_id, provider, workspace_path)
        return [r.model_dump() for r in results]

    async def pause_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Pause sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata.
        """
        meta_dict = self._repository.get_metadata(sandbox_id)
        provider = self._registry.get(meta_dict["provider"])
        await provider.pause(sandbox_id)

        result = self._repository.update_metadata(
            sandbox_id,
            status="paused",
            paused_at=datetime.now(UTC),
            expires_at=None,
        )
        logger.info("Paused sandbox %s", sandbox_id)
        return result

    async def resume_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Resume paused sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata.
        """
        meta_dict = self._repository.get_metadata(sandbox_id)
        provider = self._registry.get(meta_dict["provider"])
        ttl = meta_dict["ttl_minutes"]
        await provider.resume(sandbox_id)

        now = datetime.now(UTC)
        result = self._repository.update_metadata(
            sandbox_id,
            status="active",
            last_active_at=now,
            expires_at=now + timedelta(minutes=ttl),
            paused_at=None,
        )
        logger.info("Resumed sandbox %s", sandbox_id)
        return result

    async def stop_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Stop and destroy sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata.
        """
        meta_dict = self._repository.get_metadata(sandbox_id)
        provider = self._registry.get(meta_dict["provider"])
        await provider.destroy(sandbox_id)

        result = self._repository.update_metadata(
            sandbox_id,
            status="stopped",
            stopped_at=datetime.now(UTC),
            expires_at=None,
        )
        logger.info("Stopped sandbox %s", sandbox_id)
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
            user_id: Filter by user.
            zone_id: Filter by zone.
            agent_id: Filter by agent.
            status: Filter by status.
            verify_status: If True, verify status with provider (parallel).

        Returns:
            List of sandbox metadata dicts.
        """
        sandbox_dicts = self._repository.list_sandboxes(
            user_id=user_id,
            zone_id=zone_id,
            agent_id=agent_id,
            status=status,
        )

        if not verify_status:
            return sandbox_dicts

        # Parallel verification using asyncio.gather (Issue #2051 #13A)
        async def _verify_one(sb_dict: dict[str, Any]) -> None:
            sb_id = sb_dict["sandbox_id"]
            sb_provider = sb_dict["provider"]
            sb_status = sb_dict["status"]

            try:
                if not self._registry.has(sb_provider):
                    logger.warning(
                        "Provider '%s' not available for sandbox %s",
                        sb_provider,
                        sb_id,
                    )
                    sb_dict["verified"] = False
                    return

                provider = self._registry.get(sb_provider)
                provider_info = await provider.get_info(sb_id)
                actual_status = provider_info.status

                sb_dict["verified"] = True
                sb_dict["provider_status"] = actual_status

                if actual_status != sb_status:
                    logger.info(
                        "Status mismatch for %s: DB=%s, Provider=%s. Updating DB.",
                        sb_id,
                        sb_status,
                        actual_status,
                    )
                    updates: dict[str, Any] = {"status": actual_status}
                    if actual_status == "stopped":
                        updates["stopped_at"] = datetime.now(UTC)
                        updates["expires_at"] = None
                    self._repository.update_metadata(sb_id, **updates)
                    sb_dict["status"] = actual_status

            except SandboxNotFoundError:
                logger.warning(
                    "Sandbox %s not found in provider. Marking as stopped.", sb_id
                )
                sb_dict["verified"] = True
                sb_dict["provider_status"] = "stopped"
                if sb_status != "stopped":
                    self._repository.update_metadata(
                        sb_id,
                        status="stopped",
                        stopped_at=datetime.now(UTC),
                        expires_at=None,
                    )
                    sb_dict["status"] = "stopped"

            except Exception as e:
                logger.warning("Failed to verify status for %s: %s", sb_id, e)
                sb_dict["verified"] = False

        await asyncio.gather(*[_verify_one(sb) for sb in sandbox_dicts])
        return sandbox_dicts

    async def get_sandbox_status(self, sandbox_id: str) -> dict[str, Any]:
        """Get sandbox status and metadata.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Sandbox metadata dict.
        """
        return self._repository.get_metadata(sandbox_id)

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

        Args:
            name: User-friendly sandbox name (unique per user).
            user_id: User ID.
            zone_id: Zone ID.
            agent_id: Agent ID (optional).
            ttl_minutes: Idle timeout in minutes.
            provider: Sandbox provider.
            template_id: Provider template ID.
            verify_status: If True, verify status with provider.

        Returns:
            Sandbox metadata dict (either existing or newly created).
        """
        existing = self._repository.find_active_by_name(user_id, name)

        if existing:
            sb_id = existing["sandbox_id"]
            sb_provider = existing["provider"]

            if verify_status:
                try:
                    if self._registry.has(sb_provider):
                        provider_obj = self._registry.get(sb_provider)
                        provider_info = await provider_obj.get_info(sb_id)
                        actual_status = provider_info.status

                        if actual_status == "active":
                            existing["verified"] = True
                            existing["provider_status"] = actual_status
                            logger.info(
                                "Found and verified existing sandbox %s (name=%s, user=%s)",
                                sb_id,
                                name,
                                user_id,
                            )
                            return existing

                        logger.warning(
                            "Sandbox %s status mismatch: DB=active, Provider=%s. Creating new.",
                            sb_id,
                            actual_status,
                        )
                        self._repository.update_metadata(
                            sb_id,
                            status="stopped",
                            stopped_at=datetime.now(UTC),
                            expires_at=None,
                        )
                    else:
                        logger.warning(
                            "Provider '%s' not available for verification", sb_provider
                        )
                except SandboxNotFoundError:
                    logger.warning(
                        "Sandbox %s not found in provider. Marking as stopped and creating new.",
                        sb_id,
                    )
                    self._repository.update_metadata(
                        sb_id,
                        status="stopped",
                        stopped_at=datetime.now(UTC),
                        expires_at=None,
                    )
                except Exception as e:
                    logger.warning("Failed to verify sandbox %s: %s", sb_id, e)
            else:
                logger.info(
                    "Found existing sandbox %s (name=%s, user=%s) - not verified",
                    sb_id,
                    name,
                    user_id,
                )
                return existing

        # No active sandbox found — create new one
        logger.info(
            "No active sandbox found for name=%s, user=%s. Creating new sandbox...",
            name,
            user_id,
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
            if "already exists" not in str(e):
                raise

            # Race condition: sandbox was created between check and create
            logger.warning("Sandbox name conflict detected. Cleaning up stale sandbox...")
            try:
                stale = self._repository.find_active_by_name(user_id, name)
                if stale:
                    self._repository.update_metadata(
                        stale["sandbox_id"],
                        status="stopped",
                        stopped_at=datetime.now(UTC),
                    )
                    logger.info("Marked stale sandbox %s as stopped", stale["sandbox_id"])
            except SQLAlchemyError as db_err:
                logger.error("Database error during stale sandbox cleanup: %s", db_err)
                raise

            # Retry create with modified name
            new_name = f"{name}-{datetime.now(UTC).strftime('%H%M%S')}"
            logger.info("Retrying with name: %s", new_name)
            return await self.create_sandbox(
                name=new_name,
                user_id=user_id,
                zone_id=zone_id,
                agent_id=agent_id,
                ttl_minutes=ttl_minutes,
                provider=provider,
                template_id=template_id,
            )

    async def connect_sandbox(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,  # noqa: ARG002
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        skip_dependency_checks: bool | None = None,
    ) -> dict[str, Any]:
        """Connect and mount Nexus to a sandbox.

        Args:
            sandbox_id: Sandbox ID.
            provider: Provider name.
            sandbox_api_key: Provider API key (for user-managed sandboxes).
            mount_path: Path where Nexus will be mounted.
            nexus_url: Nexus server URL (required).
            nexus_api_key: Nexus API key (required).
            agent_id: Optional agent ID for version attribution.
            skip_dependency_checks: If True, skip install checks.

        Returns:
            Dict with connection details.
        """
        provider_obj = self._registry.get(provider)

        if not nexus_url or not nexus_api_key:
            raise ValueError("Both nexus_url and nexus_api_key required for mounting")

        # Auto-detect skip_dependency_checks based on template
        if skip_dependency_checks is None:
            try:
                meta_dict = self._repository.get_metadata(sandbox_id)
                tid = meta_dict.get("template_id")
                preinstalled_templates = {"nexus-sandbox", "nexus-fuse", "aquarius-worker"}
                if tid and any(t in tid for t in preinstalled_templates):
                    skip_dependency_checks = True
                    logger.info("Auto-skipping dependency checks for template '%s'", tid)
                else:
                    skip_dependency_checks = False
            except SandboxNotFoundError:
                skip_dependency_checks = False

        logger.info(
            "Connecting to sandbox %s (provider=%s, mount=%s, skip_checks=%s)",
            sandbox_id,
            provider,
            mount_path,
            skip_dependency_checks,
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
            logger.info(
                "Successfully mounted Nexus in sandbox %s at %s", sandbox_id, mount_path
            )
        else:
            logger.warning(
                "Failed to mount Nexus in sandbox %s: %s",
                sandbox_id,
                mount_result["message"],
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
        """Disconnect and unmount Nexus from a sandbox.

        Args:
            sandbox_id: Sandbox ID.
            provider: Provider name.
            sandbox_api_key: Provider API key.

        Returns:
            Dict with disconnection details.
        """
        provider_obj = self._registry.get(provider)

        if not sandbox_api_key:
            raise ValueError(f"Sandbox API key required for provider '{provider}'")

        logger.info(
            "Disconnecting from user-managed sandbox %s (provider=%s)",
            sandbox_id,
            provider,
        )

        now = datetime.now(UTC)

        # Execute unmount if provider supports it (Issue #2051 #6A)
        if hasattr(provider_obj, "unmount_nexus"):
            try:
                unmount_result = await provider_obj.unmount_nexus(sandbox_id)
                if not unmount_result.get("success", False):
                    logger.warning(
                        "Unmount failed for sandbox %s: %s",
                        sandbox_id,
                        unmount_result.get("message", "unknown"),
                    )
            except Exception as e:
                logger.warning("Unmount error for sandbox %s: %s", sandbox_id, e)

        logger.info("Disconnected from sandbox %s", sandbox_id)

        return {
            "success": True,
            "sandbox_id": sandbox_id,
            "provider": provider,
            "unmounted_at": now.isoformat(),
        }

    async def cleanup_expired_sandboxes(self) -> int:
        """Clean up expired sandboxes.

        Returns:
            Number of sandboxes cleaned up.
        """
        expired_ids = self._repository.find_expired()

        count = 0
        for sb_id in expired_ids:
            try:
                await self.stop_sandbox(sb_id)
                count += 1
            except Exception as e:
                logger.error("Failed to cleanup sandbox %s: %s", sb_id, e)

        if count > 0:
            logger.info("Cleaned up %d expired sandboxes", count)

        return count

    # -- Private helpers ---------------------------------------------------

    @staticmethod
    def _build_default_registry(
        e2b_api_key: str | None,
        e2b_team_id: str | None,
        e2b_template_id: str | None,
        config: Any,
    ) -> ProviderRegistry:
        """Build default ProviderRegistry with lazy provider factories.

        Provider imports and initialization are deferred until first use,
        avoiding import errors when optional dependencies are missing.

        Args:
            e2b_api_key: E2B API key.
            e2b_team_id: E2B team ID.
            e2b_template_id: Default E2B template ID.
            config: Nexus configuration.

        Returns:
            Configured ProviderRegistry.
        """
        registry = ProviderRegistry()

        # E2B provider (requires API key + package)
        if e2b_api_key:

            def _create_e2b() -> SandboxProvider:
                from nexus.bricks.sandbox.sandbox_e2b_provider import E2BSandboxProvider

                return E2BSandboxProvider(
                    api_key=e2b_api_key,
                    team_id=e2b_team_id,
                    default_template=e2b_template_id,
                )

            try:
                provider = _create_e2b()
                registry.register("e2b", provider)
                logger.info("E2B provider initialized successfully")
            except ImportError:
                logger.info(
                    "E2B provider not available (e2b_code_interpreter package not installed)"
                )
            except Exception as e:
                logger.warning("Failed to initialize E2B provider: %s", e)

        # Docker provider (no API key needed)
        try:
            from nexus.bricks.sandbox.sandbox_docker_provider import DockerSandboxProvider

            docker_config = config.docker if config and hasattr(config, "docker") else None
            provider = DockerSandboxProvider(docker_config=docker_config)
            registry.register("docker", provider)
            logger.info("Docker provider initialized successfully")
        except ImportError:
            pass
        except RuntimeError as e:
            logger.info("Docker provider not available: %s", e)
        except Exception as e:
            logger.warning("Failed to initialize Docker provider: %s", e)

        # Monty provider (no external deps, Issue #1316)
        try:
            from nexus.bricks.sandbox.sandbox_monty_provider import MontySandboxProvider

            monty_profile = "standard"
            if config and hasattr(config, "monty_resource_profile"):
                monty_profile = config.monty_resource_profile
            provider = MontySandboxProvider(resource_profile=monty_profile)
            registry.register("monty", provider)
            logger.info("Monty provider initialized successfully")
        except ImportError:
            pass
        except RuntimeError as e:
            logger.info("Monty provider not available: %s", e)
        except Exception as e:
            logger.warning("Failed to initialize Monty provider: %s", e)

        return registry

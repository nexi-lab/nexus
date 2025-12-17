"""Sandbox manager for Nexus-managed sandboxes.

Coordinates sandbox lifecycle management using providers (E2B, Docker, etc.)
and database metadata storage. Handles creation, TTL tracking, and cleanup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.core.sandbox_e2b_provider import E2BSandboxProvider
from nexus.core.sandbox_provider import (
    SandboxNotFoundError,
    SandboxProvider,
)
from nexus.storage.models import SandboxMetadataModel

# Try to import Docker provider
try:
    from nexus.core.sandbox_docker_provider import DockerSandboxProvider

    DOCKER_PROVIDER_AVAILABLE = True
except ImportError:
    DOCKER_PROVIDER_AVAILABLE = False

logger = logging.getLogger(__name__)


class SandboxManager:
    """Manages sandboxes across different providers with database persistence.

    Responsibilities:
    - Create sandboxes using providers (E2B, Docker, etc.)
    - Store metadata in database
    - Track TTL and expiry
    - Handle lifecycle operations (pause/resume/stop)
    - Clean up expired sandboxes

    Note: Providers are async. Database operations use sync sessions.
    """

    def __init__(
        self,
        db_session: Session,
        e2b_api_key: str | None = None,
        e2b_team_id: str | None = None,
        e2b_template_id: str | None = None,
        config: Any = None,  # NexusConfig | None
    ):
        """Initialize sandbox manager.

        Args:
            db_session: Database session for metadata (sync)
            e2b_api_key: E2B API key
            e2b_team_id: E2B team ID
            e2b_template_id: Default E2B template ID
            config: Nexus configuration (for Docker templates)
        """
        self.db = db_session

        # Initialize providers
        self.providers: dict[str, SandboxProvider] = {}
        if e2b_api_key:
            self.providers["e2b"] = E2BSandboxProvider(
                api_key=e2b_api_key,
                team_id=e2b_team_id,
                default_template=e2b_template_id,
            )

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

        logger.info(f"Initialized sandbox manager with providers: {list(self.providers.keys())}")

    async def create_sandbox(
        self,
        name: str,
        user_id: str,
        tenant_id: str,
        agent_id: str | None = None,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new sandbox.

        Args:
            name: User-friendly name (unique per user)
            user_id: User ID
            tenant_id: Tenant ID
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
        # Allow reusing name if existing sandbox is stopped/paused
        existing = self.db.execute(
            select(SandboxMetadataModel).where(
                SandboxMetadataModel.user_id == user_id,
                SandboxMetadataModel.name == name,
                SandboxMetadataModel.status == "active",
            )
        )
        if existing.scalar_one_or_none():
            raise ValueError(
                f"Active sandbox with name '{name}' already exists for user {user_id}. "
                f"Use sandbox_get_or_create() to reuse it or choose a different name."
            )

        # Create sandbox via provider (async call)
        provider_obj = self.providers[provider]
        sandbox_id = await provider_obj.create(
            template_id=template_id,
            timeout_minutes=ttl_minutes,
            metadata={"name": name},
        )

        # OPTIMIZATION: Start pre-warming Python imports in background
        # This runs while we do DB operations and return to user
        # By the time connect() is called, imports should be cached
        if hasattr(provider_obj, "prewarm_imports"):
            try:
                await provider_obj.prewarm_imports(sandbox_id)
            except Exception as e:
                logger.debug(f"Pre-warm failed (non-fatal): {e}")

        # Calculate expiry time
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=ttl_minutes)

        # Create database record
        metadata = SandboxMetadataModel(
            sandbox_id=sandbox_id,
            name=name,
            user_id=user_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            provider=provider,
            template_id=template_id,
            status="active",
            created_at=now,
            last_active_at=now,
            ttl_minutes=ttl_minutes,
            expires_at=expires_at,
            auto_created=1,  # PostgreSQL integer type
        )

        self.db.add(metadata)
        self.db.commit()
        self.db.refresh(metadata)

        logger.info(
            f"Created sandbox {sandbox_id} (name={name}, user={user_id}, provider={provider})"
        )

        return self._metadata_to_dict(metadata)

    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
    ) -> dict[str, Any]:
        """Run code in sandbox.

        Args:
            sandbox_id: Sandbox ID
            language: Programming language
            code: Code to execute
            timeout: Timeout in seconds

        Returns:
            Dict with stdout, stderr, exit_code, execution_time

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        # Get metadata
        metadata = self._get_metadata(sandbox_id)

        # Run code via provider
        provider = self.providers[metadata.provider]
        result = await provider.run_code(sandbox_id, language, code, timeout)

        # Update last_active_at and expires_at
        now = datetime.now(UTC)
        metadata.last_active_at = now
        metadata.expires_at = now + timedelta(minutes=metadata.ttl_minutes)
        self.db.commit()

        logger.debug(f"Executed {language} code in sandbox {sandbox_id}")

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "execution_time": result.execution_time,
        }

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
        metadata = self._get_metadata(sandbox_id)

        # Pause via provider
        provider = self.providers[metadata.provider]
        await provider.pause(sandbox_id)

        # Update metadata
        metadata.status = "paused"
        metadata.paused_at = datetime.now(UTC)
        metadata.expires_at = None  # Don't expire while paused
        self.db.commit()
        self.db.refresh(metadata)

        logger.info(f"Paused sandbox {sandbox_id}")
        return self._metadata_to_dict(metadata)

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
        metadata = self._get_metadata(sandbox_id)

        # Resume via provider
        provider = self.providers[metadata.provider]
        await provider.resume(sandbox_id)

        # Update metadata
        now = datetime.now(UTC)
        metadata.status = "active"
        metadata.last_active_at = now
        metadata.expires_at = now + timedelta(minutes=metadata.ttl_minutes)
        metadata.paused_at = None
        self.db.commit()
        self.db.refresh(metadata)

        logger.info(f"Resumed sandbox {sandbox_id}")
        return self._metadata_to_dict(metadata)

    async def stop_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Stop and destroy sandbox.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Updated sandbox metadata

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        metadata = self._get_metadata(sandbox_id)

        # Destroy via provider
        provider = self.providers[metadata.provider]
        await provider.destroy(sandbox_id)

        # Update metadata
        metadata.status = "stopped"
        metadata.stopped_at = datetime.now(UTC)
        metadata.expires_at = None
        self.db.commit()
        self.db.refresh(metadata)

        logger.info(f"Stopped sandbox {sandbox_id}")
        return self._metadata_to_dict(metadata)

    async def list_sandboxes(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        verify_status: bool = False,
    ) -> list[dict[str, Any]]:
        """List sandboxes with optional filtering.

        Args:
            user_id: Filter by user (optional)
            tenant_id: Filter by tenant (optional)
            agent_id: Filter by agent (optional)
            status: Filter by status (e.g., 'active', 'stopped', 'paused') (optional)
            verify_status: If True, verify status with provider (slower but accurate)

        Returns:
            List of sandbox metadata dicts
        """
        query = select(SandboxMetadataModel)

        if user_id:
            query = query.where(SandboxMetadataModel.user_id == user_id)
        if tenant_id:
            query = query.where(SandboxMetadataModel.tenant_id == tenant_id)
        if agent_id:
            query = query.where(SandboxMetadataModel.agent_id == agent_id)
        if status:
            query = query.where(SandboxMetadataModel.status == status)

        result = self.db.execute(query)
        sandboxes = result.scalars().all()

        # Convert to dicts
        sandbox_dicts = [self._metadata_to_dict(sb) for sb in sandboxes]

        # Verify status with provider if requested
        if verify_status:
            for i, metadata in enumerate(sandboxes):
                try:
                    # Get provider for this sandbox
                    provider = self.providers.get(metadata.provider)
                    if not provider:
                        logger.warning(
                            f"Provider '{metadata.provider}' not available for sandbox {metadata.sandbox_id}"
                        )
                        sandbox_dicts[i]["verified"] = False
                        continue

                    # Get actual status from provider
                    provider_info = await provider.get_info(metadata.sandbox_id)
                    actual_status = provider_info.status

                    # Update dict with verified status
                    sandbox_dicts[i]["verified"] = True
                    sandbox_dicts[i]["provider_status"] = actual_status

                    # Update database if status has changed
                    if actual_status != metadata.status:
                        logger.info(
                            f"Status mismatch for {metadata.sandbox_id}: "
                            f"DB={metadata.status}, Provider={actual_status}. Updating DB."
                        )
                        metadata.status = actual_status
                        # Update stopped_at if provider shows stopped
                        if actual_status == "stopped" and not metadata.stopped_at:
                            metadata.stopped_at = datetime.now(UTC)
                            metadata.expires_at = None
                        self.db.commit()
                        sandbox_dicts[i]["status"] = actual_status

                except SandboxNotFoundError:
                    # Sandbox doesn't exist in provider anymore
                    logger.warning(
                        f"Sandbox {metadata.sandbox_id} not found in provider. Marking as stopped."
                    )
                    sandbox_dicts[i]["verified"] = True
                    sandbox_dicts[i]["provider_status"] = "stopped"

                    if metadata.status != "stopped":
                        metadata.status = "stopped"
                        metadata.stopped_at = datetime.now(UTC)
                        metadata.expires_at = None
                        self.db.commit()
                        sandbox_dicts[i]["status"] = "stopped"

                except Exception as e:
                    logger.warning(f"Failed to verify status for {metadata.sandbox_id}: {e}")
                    sandbox_dicts[i]["verified"] = False

        return sandbox_dicts

    async def get_sandbox_status(self, sandbox_id: str) -> dict[str, Any]:
        """Get sandbox status and metadata.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox metadata dict

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        metadata = self._get_metadata(sandbox_id)
        return self._metadata_to_dict(metadata)

    async def get_or_create_sandbox(
        self,
        name: str,
        user_id: str,
        tenant_id: str,
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
            tenant_id: Tenant ID
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
        # This avoids verifying unrelated sandboxes (saves ~18s)
        result = self.db.execute(
            select(SandboxMetadataModel).where(
                SandboxMetadataModel.user_id == user_id,
                SandboxMetadataModel.name == name,
                SandboxMetadataModel.status == "active",
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            sandbox_dict = self._metadata_to_dict(existing)

            if verify_status:
                # Verify ONLY this specific sandbox with provider
                try:
                    provider_obj = self.providers.get(existing.provider)
                    if provider_obj:
                        provider_info = await provider_obj.get_info(existing.sandbox_id)
                        actual_status = provider_info.status

                        if actual_status == "active":
                            # Sandbox is alive, return it
                            sandbox_dict["verified"] = True
                            sandbox_dict["provider_status"] = actual_status
                            logger.info(
                                f"Found and verified existing sandbox {existing.sandbox_id} "
                                f"(name={name}, user={user_id})"
                            )
                            return sandbox_dict
                        else:
                            # Sandbox is dead, update DB and create new
                            logger.warning(
                                f"Sandbox {existing.sandbox_id} status mismatch: "
                                f"DB=active, Provider={actual_status}. Creating new."
                            )
                            existing.status = "stopped"
                            existing.stopped_at = datetime.now(UTC)
                            existing.expires_at = None
                            self.db.commit()
                    else:
                        logger.warning(
                            f"Provider '{existing.provider}' not available for verification"
                        )
                except SandboxNotFoundError:
                    # Sandbox doesn't exist in provider, mark as stopped
                    logger.warning(
                        f"Sandbox {existing.sandbox_id} not found in provider. "
                        f"Marking as stopped and creating new."
                    )
                    existing.status = "stopped"
                    existing.stopped_at = datetime.now(UTC)
                    existing.expires_at = None
                    self.db.commit()
                except Exception as e:
                    logger.warning(f"Failed to verify sandbox {existing.sandbox_id}: {e}")
            else:
                # No verification requested - use cached status
                logger.info(
                    f"Found existing sandbox {existing.sandbox_id} "
                    f"(name={name}, user={user_id}) - not verified"
                )
                return sandbox_dict

        # No active sandbox found - create new one
        logger.info(
            f"No active sandbox found for name={name}, user={user_id}. Creating new sandbox..."
        )

        try:
            return await self.create_sandbox(
                name=name,
                user_id=user_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                ttl_minutes=ttl_minutes,
                provider=provider,
                template_id=template_id,
            )
        except ValueError as e:
            if "already exists" in str(e):
                # Race condition: sandbox was created between check and create
                logger.warning("Sandbox name conflict detected. Cleaning up stale sandbox...")
                result = self.db.execute(
                    select(SandboxMetadataModel).where(
                        SandboxMetadataModel.user_id == user_id,
                        SandboxMetadataModel.name == name,
                    )
                )
                stale = result.scalar_one_or_none()
                if stale:
                    stale.status = "stopped"
                    stale.stopped_at = datetime.now(UTC)
                    self.db.commit()
                    logger.info(f"Marked stale sandbox {stale.sandbox_id} as stopped")

                # Retry create with modified name
                new_name = f"{name}-{datetime.now(UTC).strftime('%H%M%S')}"
                logger.info(f"Retrying with name: {new_name}")
                return await self.create_sandbox(
                    name=new_name,
                    user_id=user_id,
                    tenant_id=tenant_id,
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
        # Known templates with pre-installed dependencies don't need checks (saves ~10s)
        if skip_dependency_checks is None:
            # Check if this sandbox uses a known template with pre-installed deps
            try:
                metadata = self._get_metadata(sandbox_id)
                template_id = metadata.template_id
                # Templates known to have nexus/fusepy pre-installed
                preinstalled_templates = {"nexus-sandbox", "nexus-fuse", "aquarius-worker"}
                if template_id and any(t in template_id for t in preinstalled_templates):
                    skip_dependency_checks = True
                    logger.info(f"Auto-skipping dependency checks for template '{template_id}'")
                else:
                    skip_dependency_checks = False
            except SandboxNotFoundError:
                # External sandbox, can't determine template - be safe and check deps
                skip_dependency_checks = False

        logger.info(
            f"Connecting to sandbox {sandbox_id} (provider={provider}, mount={mount_path}, "
            f"skip_checks={skip_dependency_checks})"
        )

        # Mount Nexus in the sandbox
        # The provider's mount_nexus will handle connecting to the sandbox
        # (either from cache for Nexus-managed, or reconnecting for user-managed)
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

        # Find expired sandboxes
        result = self.db.execute(
            select(SandboxMetadataModel).where(
                SandboxMetadataModel.status == "active",
                SandboxMetadataModel.expires_at < now,
            )
        )
        expired = result.scalars().all()

        count = 0
        for metadata in expired:
            try:
                await self.stop_sandbox(metadata.sandbox_id)
                count += 1
            except Exception as e:
                logger.error(f"Failed to cleanup sandbox {metadata.sandbox_id}: {e}")

        if count > 0:
            logger.info(f"Cleaned up {count} expired sandboxes")

        return count

    def _get_metadata(self, sandbox_id: str) -> SandboxMetadataModel:
        """Get sandbox metadata from database.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox metadata

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        result = self.db.execute(
            select(SandboxMetadataModel).where(SandboxMetadataModel.sandbox_id == sandbox_id)
        )
        metadata = result.scalar_one_or_none()

        if not metadata:
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")

        return metadata

    def _metadata_to_dict(self, metadata: SandboxMetadataModel) -> dict[str, Any]:
        """Convert metadata model to dict.

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
            "tenant_id": metadata.tenant_id,
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

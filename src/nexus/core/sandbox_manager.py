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
    ):
        """Initialize sandbox manager.

        Args:
            db_session: Database session for metadata (sync)
            e2b_api_key: E2B API key
            e2b_team_id: E2B team ID
            e2b_template_id: Default E2B template ID
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
                self.providers["docker"] = DockerSandboxProvider()
                logger.info("Docker provider initialized successfully")
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
        provider: str = "e2b",
        template_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new sandbox.

        Args:
            name: User-friendly name (unique per user)
            user_id: User ID
            tenant_id: Tenant ID
            agent_id: Agent ID (optional)
            ttl_minutes: Idle timeout in minutes
            provider: Provider name ("e2b", "docker", etc.)
            template_id: Template ID for provider

        Returns:
            Sandbox metadata dict with sandbox_id, name, status, etc.

        Raises:
            ValueError: If provider not available or name already exists
            SandboxCreationError: If sandbox creation fails
        """
        # Check provider availability
        if provider not in self.providers:
            available = ", ".join(self.providers.keys())
            raise ValueError(f"Provider '{provider}' not available. Available: {available}")

        # Check name uniqueness
        existing = self.db.execute(
            select(SandboxMetadataModel).where(
                SandboxMetadataModel.user_id == user_id,
                SandboxMetadataModel.name == name,
            )
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"Sandbox with name '{name}' already exists for user {user_id}")

        # Create sandbox via provider (async call)
        provider_obj = self.providers[provider]
        sandbox_id = await provider_obj.create(
            template_id=template_id,
            timeout_minutes=ttl_minutes,
        )

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
        timeout: int = 30,
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
    ) -> list[dict[str, Any]]:
        """List sandboxes with optional filtering.

        Args:
            user_id: Filter by user (optional)
            tenant_id: Filter by tenant (optional)
            agent_id: Filter by agent (optional)

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

        result = self.db.execute(query)
        sandboxes = result.scalars().all()

        return [self._metadata_to_dict(sb) for sb in sandboxes]

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

    async def connect_sandbox(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,  # noqa: ARG002 - Reserved for user-managed sandboxes
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
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

        Returns:
            Dict with connection details (sandbox_id, provider, mount_path, mounted_at, mount_status)

        Raises:
            ValueError: If provider not available or required credentials missing
            RuntimeError: If connection/mount fails
        """
        # Check provider availability
        if provider not in self.providers:
            available = ", ".join(self.providers.keys())
            raise ValueError(f"Provider '{provider}' not available. Available: {available}")

        if not nexus_url or not nexus_api_key:
            raise ValueError("Both nexus_url and nexus_api_key required for mounting")

        # Get provider
        provider_obj = self.providers[provider]

        logger.info(f"Connecting to sandbox {sandbox_id} (provider={provider}, mount={mount_path})")

        # Mount Nexus in the sandbox
        # The provider's mount_nexus will handle connecting to the sandbox
        # (either from cache for Nexus-managed, or reconnecting for user-managed)
        mount_result = await provider_obj.mount_nexus(
            sandbox_id=sandbox_id,
            mount_path=mount_path,
            nexus_url=nexus_url,
            api_key=nexus_api_key,
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
            available = ", ".join(self.providers.keys())
            raise ValueError(f"Provider '{provider}' not available. Available: {available}")

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

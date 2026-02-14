"""Share Link Service - Extracted from NexusFSShareLinksMixin.

This service handles all share link operations:
- Create share links with configurable access (viewer/editor/owner)
- Password protection, expiration, and download limits
- Access validation and logging
- Revocation support

Implements W3C TAG Capability URL pattern for secure file sharing.

Phase 2: Core Refactoring (Issue #1287)
Extracted from: nexus_fs_share_links.py (678 lines)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from nexus.core.path_utils import validate_path
from nexus.core.response import HandlerResponse
from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.services.gateway import NexusFSGateway


class ShareLinkService:
    """Independent share link service extracted from NexusFS.

    Implements W3C TAG Capability URL pattern:
    - Share links are unguessable tokens (UUID v4, 122 bits entropy)
    - Token IS the credential - no session/ReBAC for anonymous access
    - Server validates token against DB on each request

    Architecture:
        - Uses Gateway for NexusFS filesystem operations
        - Uses session_factory for ShareLinkModel database access
        - Permission checking via Gateway's rebac operations
        - Clean dependency injection
    """

    def __init__(
        self,
        gateway: NexusFSGateway,
        enforce_permissions: bool = True,
    ):
        """Initialize share link service.

        Args:
            gateway: NexusFSGateway for filesystem and DB access
            enforce_permissions: Whether to enforce permission checks
        """
        self._gw = gateway
        self._enforce_permissions = enforce_permissions
        logger.info("[ShareLinkService] Initialized")

    # =========================================================================
    # Password Hashing
    # =========================================================================

    @staticmethod
    def _hash_password(password: str) -> str:
        """Hash password for share link protection.

        Uses SHA-256 with random salt.

        Args:
            password: Plain text password

        Returns:
            Hashed password string in format "salt:hash"
        """
        salt = secrets.token_hex(16)
        hash_input = f"{salt}:{password}".encode()
        password_hash = hashlib.sha256(hash_input).hexdigest()
        return f"{salt}:{password_hash}"

    @staticmethod
    def _verify_password(password: str, password_hash: str) -> bool:
        """Verify password against stored hash.

        Args:
            password: Plain text password to verify
            password_hash: Stored hash in format "salt:hash"

        Returns:
            True if password matches
        """
        try:
            salt, stored_hash = password_hash.split(":", 1)
            hash_input = f"{salt}:{password}".encode()
            computed_hash = hashlib.sha256(hash_input).hexdigest()
            return secrets.compare_digest(computed_hash, stored_hash)
        except (ValueError, AttributeError):
            return False

    # =========================================================================
    # Context Extraction Helper
    # =========================================================================

    @staticmethod
    def _extract_context_info(
        context: OperationContext | None,
    ) -> tuple[str, str, bool]:
        """Extract zone_id, user_id, is_admin from context.

        Args:
            context: Operation context

        Returns:
            Tuple of (zone_id, user_id, is_admin)
        """
        zone_id = "default"
        user_id = "anonymous"
        is_admin = False
        if context:
            zone_id = getattr(context, "zone_id", None) or "default"
            user_id = (
                getattr(context, "user", None)
                or getattr(context, "subject_id", None)
                or "anonymous"
            )
            is_admin = getattr(context, "is_admin", False)
        return zone_id, user_id, is_admin

    # =========================================================================
    # Public API
    # =========================================================================

    @rpc_expose(description="Create a share link for a file or directory")
    async def create_share_link(
        self,
        path: str,
        permission_level: str = "viewer",
        expires_in_hours: int | None = None,
        max_access_count: int | None = None,
        password: str | None = None,
        context: OperationContext | None = None,
    ) -> HandlerResponse:
        """Create a shareable link for a file or directory.

        Args:
            path: Virtual path to share
            permission_level: Access level - 'viewer', 'editor', or 'owner'
            expires_in_hours: Optional hours until link expires
            max_access_count: Optional max number of accesses
            password: Optional password protection
            context: Operation context with user/zone info

        Returns:
            HandlerResponse with link_id and share URL on success
        """

        def _impl() -> HandlerResponse:
            from nexus.storage.models import ShareLinkModel

            valid_levels = {"viewer", "editor", "owner"}
            if permission_level not in valid_levels:
                return HandlerResponse.error(
                    f"Invalid permission_level '{permission_level}'. Must be one of: {valid_levels}",
                    code=400,
                    is_expected=True,
                )

            try:
                normalized_path = validate_path(path, allow_root=True)
            except Exception as e:
                return HandlerResponse.error(f"Invalid path: {e}", code=400, is_expected=True)

            zone_id, created_by, _ = self._extract_context_info(context)

            # Check write permission to create share link
            if self._enforce_permissions and context:
                has_perm = self._gw.rebac_check(
                    subject=("user", created_by),
                    permission="write",
                    object=("file", normalized_path),
                    zone_id=zone_id,
                )
                if not has_perm:
                    return HandlerResponse.error(
                        f"Permission denied: cannot create share link for '{path}'",
                        code=403,
                        is_expected=True,
                    )

            # Determine resource type
            resource_type = "file"
            if self._gw.exists(normalized_path):
                meta = self._gw.metadata_get(normalized_path)
                if meta and getattr(meta, "is_dir", False):
                    resource_type = "directory"

            # Calculate expiration
            expires_at = None
            if expires_in_hours is not None and expires_in_hours > 0:
                expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)

            # Hash password if provided
            password_hash = None
            if password:
                password_hash = self._hash_password(password)

            # Create share link record
            session_factory = self._gw.session_factory
            if session_factory is None:
                return HandlerResponse.error("Database not configured for share links", code=500)

            try:
                with session_factory() as session:
                    share_link = ShareLinkModel(
                        resource_type=resource_type,
                        resource_id=normalized_path,
                        permission_level=permission_level,
                        zone_id=zone_id,
                        created_by=created_by,
                        password_hash=password_hash,
                        expires_at=expires_at,
                        max_access_count=max_access_count,
                        access_count=0,
                    )
                    session.add(share_link)
                    session.commit()

                    return HandlerResponse.ok(
                        {
                            "link_id": share_link.link_id,
                            "path": normalized_path,
                            "permission_level": permission_level,
                            "resource_type": resource_type,
                            "expires_at": expires_at.isoformat() if expires_at else None,
                            "max_access_count": max_access_count,
                            "has_password": password_hash is not None,
                            "created_at": share_link.created_at.isoformat(),
                        }
                    )
            except Exception as e:
                return HandlerResponse.error(f"Failed to create share link: {e}", code=500)

        return await asyncio.to_thread(_impl)

    @rpc_expose(description="Get details of a share link")
    async def get_share_link(
        self,
        link_id: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse:
        """Get details of a share link.

        Args:
            link_id: The share link ID/token
            context: Operation context (used for authorization)

        Returns:
            HandlerResponse with link details
        """

        def _impl() -> HandlerResponse:
            from nexus.storage.models import ShareLinkModel

            session_factory = self._gw.session_factory
            if session_factory is None:
                return HandlerResponse.error("Database not configured", code=500)

            try:
                with session_factory() as session:
                    link = session.query(ShareLinkModel).filter_by(link_id=link_id).first()
                    if not link:
                        return HandlerResponse.error(
                            f"Share link not found: {link_id}", code=404, is_expected=True
                        )

                    zone_id, user_id, is_admin = self._extract_context_info(context)
                    is_owner = link.created_by == user_id and link.zone_id == zone_id

                    if not is_owner and not is_admin:
                        return HandlerResponse.ok(
                            {
                                "link_id": link.link_id,
                                "resource_type": link.resource_type,
                                "permission_level": link.permission_level,
                                "is_valid": link.is_valid(),
                                "has_password": link.password_hash is not None,
                            }
                        )

                    return HandlerResponse.ok(
                        {
                            "link_id": link.link_id,
                            "path": link.resource_id,
                            "resource_type": link.resource_type,
                            "permission_level": link.permission_level,
                            "zone_id": link.zone_id,
                            "created_by": link.created_by,
                            "created_at": link.created_at.isoformat(),
                            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
                            "max_access_count": link.max_access_count,
                            "access_count": link.access_count,
                            "last_accessed_at": (
                                link.last_accessed_at.isoformat() if link.last_accessed_at else None
                            ),
                            "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
                            "revoked_by": link.revoked_by,
                            "has_password": link.password_hash is not None,
                            "is_valid": link.is_valid(),
                        }
                    )
            except Exception as e:
                return HandlerResponse.error(f"Failed to get share link: {e}", code=500)

        return await asyncio.to_thread(_impl)

    @rpc_expose(description="List share links created by the current user")
    async def list_share_links(
        self,
        path: str | None = None,
        include_revoked: bool = False,
        include_expired: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse:
        """List share links created by the current user.

        Args:
            path: Optional - filter by resource path
            include_revoked: Include revoked links
            include_expired: Include expired links
            context: Operation context with user/zone info

        Returns:
            HandlerResponse with list of share links
        """

        def _impl() -> HandlerResponse:
            from nexus.storage.models import ShareLinkModel

            session_factory = self._gw.session_factory
            if session_factory is None:
                return HandlerResponse.error("Database not configured", code=500)

            zone_id, user_id, is_admin = self._extract_context_info(context)

            try:
                with session_factory() as session:
                    query = session.query(ShareLinkModel).filter_by(zone_id=zone_id)

                    if not is_admin:
                        query = query.filter_by(created_by=user_id)

                    if path:
                        normalized_path = validate_path(path, allow_root=True)
                        query = query.filter_by(resource_id=normalized_path)

                    if not include_revoked:
                        query = query.filter(ShareLinkModel.revoked_at.is_(None))

                    if not include_expired:
                        now = datetime.now(UTC)
                        query = query.filter(
                            (ShareLinkModel.expires_at.is_(None))
                            | (ShareLinkModel.expires_at >= now)
                        )

                    query = query.order_by(ShareLinkModel.created_at.desc())
                    links = query.all()

                    result = [
                        {
                            "link_id": link.link_id,
                            "path": link.resource_id,
                            "resource_type": link.resource_type,
                            "permission_level": link.permission_level,
                            "created_at": link.created_at.isoformat(),
                            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
                            "max_access_count": link.max_access_count,
                            "access_count": link.access_count,
                            "has_password": link.password_hash is not None,
                            "is_valid": link.is_valid(),
                            "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
                        }
                        for link in links
                    ]

                    return HandlerResponse.ok({"links": result, "count": len(result)})
            except Exception as e:
                return HandlerResponse.error(f"Failed to list share links: {e}", code=500)

        return await asyncio.to_thread(_impl)

    @rpc_expose(description="Revoke a share link")
    async def revoke_share_link(
        self,
        link_id: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse:
        """Revoke a share link, immediately disabling access.

        Args:
            link_id: The share link ID to revoke
            context: Operation context with user/zone info

        Returns:
            HandlerResponse indicating success/failure
        """

        def _impl() -> HandlerResponse:
            from nexus.storage.models import ShareLinkModel

            session_factory = self._gw.session_factory
            if session_factory is None:
                return HandlerResponse.error("Database not configured", code=500)

            zone_id, user_id, is_admin = self._extract_context_info(context)

            try:
                with session_factory() as session:
                    link = session.query(ShareLinkModel).filter_by(link_id=link_id).first()
                    if not link:
                        return HandlerResponse.error(
                            f"Share link not found: {link_id}", code=404, is_expected=True
                        )

                    is_owner = link.created_by == user_id and link.zone_id == zone_id
                    if not is_owner and not is_admin:
                        return HandlerResponse.error(
                            "Permission denied: only link creator or admin can revoke",
                            code=403,
                            is_expected=True,
                        )

                    if link.revoked_at is not None:
                        return HandlerResponse.error(
                            "Share link is already revoked", code=400, is_expected=True
                        )

                    link.revoked_at = datetime.now(UTC)
                    link.revoked_by = user_id
                    session.commit()

                    return HandlerResponse.ok(
                        {
                            "link_id": link_id,
                            "revoked": True,
                            "revoked_at": link.revoked_at.isoformat(),
                            "revoked_by": link.revoked_by,
                        }
                    )
            except Exception as e:
                return HandlerResponse.error(f"Failed to revoke share link: {e}", code=500)

        return await asyncio.to_thread(_impl)

    @rpc_expose(description="Access a shared resource via share link")
    async def access_share_link(
        self,
        link_id: str,
        password: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        context: OperationContext | None = None,
    ) -> HandlerResponse:
        """Validate and access a shared resource via share link.

        This method:
        1. Validates the share link (exists, not expired, not revoked, not over limit)
        2. Verifies password if required
        3. Logs the access attempt
        4. Returns resource access info if valid

        Args:
            link_id: The share link token
            password: Password if link is password-protected
            ip_address: Client IP for logging
            user_agent: Client user agent for logging
            context: Optional operation context (for authenticated access)

        Returns:
            HandlerResponse with resource access info or error
        """

        def _impl() -> HandlerResponse:
            from nexus.storage.models import ShareLinkAccessLogModel, ShareLinkModel

            session_factory = self._gw.session_factory
            if session_factory is None:
                return HandlerResponse.error("Database not configured", code=500)

            accessed_by_user_id = None
            accessed_by_zone_id = None
            if context:
                accessed_by_user_id = getattr(context, "user", None) or getattr(
                    context, "subject_id", None
                )
                accessed_by_zone_id = getattr(context, "zone_id", None)

            try:
                with session_factory() as session:
                    link = session.query(ShareLinkModel).filter_by(link_id=link_id).first()

                    def log_access(success: bool, failure_reason: str | None = None) -> None:
                        log_entry = ShareLinkAccessLogModel(
                            link_id=link_id,
                            ip_address=ip_address,
                            user_agent=user_agent,
                            success=1 if success else 0,
                            failure_reason=failure_reason,
                            accessed_by_user_id=accessed_by_user_id,
                            accessed_by_zone_id=accessed_by_zone_id,
                        )
                        session.add(log_entry)

                    if not link:
                        return HandlerResponse.error(
                            "Share link not found or invalid", code=404, is_expected=True
                        )

                    if link.revoked_at is not None:
                        log_access(False, "revoked")
                        session.commit()
                        return HandlerResponse.error(
                            "Share link has been revoked", code=403, is_expected=True
                        )

                    now = datetime.now(UTC)
                    if link.expires_at is not None and link.expires_at < now:
                        log_access(False, "expired")
                        session.commit()
                        return HandlerResponse.error(
                            "Share link has expired", code=410, is_expected=True
                        )

                    if (
                        link.max_access_count is not None
                        and link.access_count >= link.max_access_count
                    ):
                        log_access(False, "limit_exceeded")
                        session.commit()
                        return HandlerResponse.error(
                            "Share link access limit exceeded", code=429, is_expected=True
                        )

                    if link.password_hash is not None:
                        if not password:
                            log_access(False, "password_required")
                            session.commit()
                            return HandlerResponse.error(
                                "Password required for this share link",
                                code=401,
                                is_expected=True,
                            )
                        if not self._verify_password(password, link.password_hash):
                            log_access(False, "wrong_password")
                            session.commit()
                            return HandlerResponse.error(
                                "Incorrect password", code=401, is_expected=True
                            )

                    link.access_count += 1
                    link.last_accessed_at = now
                    log_access(True)
                    session.commit()

                    return HandlerResponse.ok(
                        {
                            "link_id": link.link_id,
                            "path": link.resource_id,
                            "resource_type": link.resource_type,
                            "permission_level": link.permission_level,
                            "zone_id": link.zone_id,
                            "access_granted": True,
                            "remaining_accesses": (
                                link.max_access_count - link.access_count
                                if link.max_access_count
                                else None
                            ),
                            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
                        }
                    )
            except Exception as e:
                return HandlerResponse.error(f"Failed to access share link: {e}", code=500)

        return await asyncio.to_thread(_impl)

    @rpc_expose(description="Get access logs for a share link")
    async def get_share_link_access_logs(
        self,
        link_id: str,
        limit: int = 100,
        context: OperationContext | None = None,
    ) -> HandlerResponse:
        """Get access logs for a share link.

        Args:
            link_id: The share link ID
            limit: Maximum number of log entries to return
            context: Operation context (must be owner or admin)

        Returns:
            HandlerResponse with access log entries
        """

        def _impl() -> HandlerResponse:
            from nexus.storage.models import ShareLinkAccessLogModel, ShareLinkModel

            session_factory = self._gw.session_factory
            if session_factory is None:
                return HandlerResponse.error("Database not configured", code=500)

            zone_id, user_id, is_admin = self._extract_context_info(context)

            try:
                with session_factory() as session:
                    link = session.query(ShareLinkModel).filter_by(link_id=link_id).first()
                    if not link:
                        return HandlerResponse.error(
                            f"Share link not found: {link_id}", code=404, is_expected=True
                        )

                    is_owner = link.created_by == user_id and link.zone_id == zone_id
                    if not is_owner and not is_admin:
                        return HandlerResponse.error(
                            "Permission denied: only link creator or admin can view logs",
                            code=403,
                            is_expected=True,
                        )

                    logs = (
                        session.query(ShareLinkAccessLogModel)
                        .filter_by(link_id=link_id)
                        .order_by(ShareLinkAccessLogModel.accessed_at.desc())
                        .limit(limit)
                        .all()
                    )

                    result = [
                        {
                            "log_id": log.log_id,
                            "accessed_at": log.accessed_at.isoformat(),
                            "ip_address": log.ip_address,
                            "user_agent": log.user_agent,
                            "success": bool(log.success),
                            "failure_reason": log.failure_reason,
                            "accessed_by_user_id": log.accessed_by_user_id,
                            "accessed_by_zone_id": log.accessed_by_zone_id,
                        }
                        for log in logs
                    ]

                    return HandlerResponse.ok({"logs": result, "count": len(result)})
            except Exception as e:
                return HandlerResponse.error(f"Failed to get access logs: {e}", code=500)

        return await asyncio.to_thread(_impl)

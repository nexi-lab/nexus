"""Share Link operations mixin for NexusFS.

Implements W3C TAG Capability URL pattern for secure file sharing:
- Anonymous access via unguessable URLs
- Optional password protection
- Time-limited access
- Download limits
- Revocation support
- Access logging

Issue #227: Document Sharing & Access Links
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from nexus.core.response import HandlerResponse
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext, Permission
    from nexus.storage import SQLAlchemyMetadataStore


class NexusFSShareLinksMixin:
    """Mixin providing share link operations for NexusFS.

    Implements Capability URL pattern (W3C TAG best practices):
    - Share links are unguessable tokens (UUID v4, 122 bits entropy)
    - Token IS the credential - no session/ReBAC for anonymous access
    - Server validates token against DB on each request
    """

    # Type hints for attributes provided by NexusFS parent class
    if TYPE_CHECKING:
        metadata: SQLAlchemyMetadataStore
        _enforce_permissions: bool

        def _validate_path(self, path: str) -> str: ...
        def _check_permission(
            self,
            path: str,
            permission: Permission,
            context: OperationContext | None = None,
        ) -> None: ...

    def _hash_share_link_password(self, password: str) -> str:
        """Hash password for share link protection.

        Uses SHA-256 with salt for simplicity. For production,
        consider Argon2id via passlib.

        Args:
            password: Plain text password

        Returns:
            Hashed password string
        """
        # Simple salted hash - in production use argon2id
        salt = secrets.token_hex(16)
        hash_input = f"{salt}:{password}".encode()
        password_hash = hashlib.sha256(hash_input).hexdigest()
        return f"{salt}:{password_hash}"

    def _verify_share_link_password(self, password: str, password_hash: str) -> bool:
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

    @rpc_expose(description="Create a share link for a file or directory")
    def create_share_link(
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
            permission_level: Access level - 'viewer' (read), 'editor' (read+write), 'owner' (full)
            expires_in_hours: Optional hours until link expires (None = never)
            max_access_count: Optional max number of accesses (None = unlimited)
            password: Optional password protection
            context: Operation context with user/zone info

        Returns:
            HandlerResponse with link_id and share URL on success
        """
        from nexus.storage.models import ShareLinkModel

        # Validate permission level
        valid_levels = {"viewer", "editor", "owner"}
        if permission_level not in valid_levels:
            return HandlerResponse.error(
                f"Invalid permission_level '{permission_level}'. Must be one of: {valid_levels}",
                code=400,
                is_expected=True,
            )

        # Validate path exists
        try:
            normalized_path = self._validate_path(path)
        except Exception as e:
            return HandlerResponse.error(f"Invalid path: {e}", code=400, is_expected=True)

        # Get zone_id and user_id from context
        zone_id = "default"
        created_by = "anonymous"
        if context:
            zone_id = getattr(context, "zone_id", None) or "default"
            created_by = (
                getattr(context, "user", None)
                or getattr(context, "subject_id", None)
                or "anonymous"
            )

        # Check if user has permission to share (must have at least the permission they're granting)
        # Owner can grant any level, editor can grant editor/viewer, viewer can grant viewer
        if self._enforce_permissions and context:
            # Check user has owner/write permission to share
            try:
                from nexus.core.permissions import Permission

                self._check_permission(normalized_path, Permission.WRITE, context)
            except PermissionError:
                return HandlerResponse.error(
                    f"Permission denied: cannot create share link for '{path}'",
                    code=403,
                    is_expected=True,
                )

        # Determine resource type
        # Check if path is a directory or file
        resource_type = "file"
        try:
            stat_result = self.stat(normalized_path)  # type: ignore[attr-defined]
            if stat_result and stat_result.get("is_dir"):
                resource_type = "directory"
        except Exception:
            pass  # Default to file if stat fails

        # Calculate expiration
        expires_at = None
        if expires_in_hours is not None and expires_in_hours > 0:
            expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)

        # Hash password if provided
        password_hash = None
        if password:
            password_hash = self._hash_share_link_password(password)

        # Create share link record
        try:
            with self.SessionLocal() as session:
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

                link_id = share_link.link_id

                return HandlerResponse.ok(
                    {
                        "link_id": link_id,
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

    @rpc_expose(description="Get details of a share link")
    def get_share_link(
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
        from nexus.storage.models import ShareLinkModel

        try:
            with self.SessionLocal() as session:
                link = session.query(ShareLinkModel).filter_by(link_id=link_id).first()

                if not link:
                    return HandlerResponse.error(
                        f"Share link not found: {link_id}", code=404, is_expected=True
                    )

                # Check authorization - only creator or admin can see full details
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

                # Check ownership
                is_owner = link.created_by == user_id and link.zone_id == zone_id

                if not is_owner and not is_admin:
                    # Return limited info for non-owners
                    return HandlerResponse.ok(
                        {
                            "link_id": link.link_id,
                            "resource_type": link.resource_type,
                            "permission_level": link.permission_level,
                            "is_valid": link.is_valid(),
                            "has_password": link.password_hash is not None,
                        }
                    )

                # Full details for owner/admin
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

    @rpc_expose(description="List share links created by the current user")
    def list_share_links(
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

        from nexus.storage.models import ShareLinkModel

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

        try:
            with self.SessionLocal() as session:
                # Base query
                query = session.query(ShareLinkModel).filter_by(zone_id=zone_id)

                # Non-admins can only see their own links
                if not is_admin:
                    query = query.filter_by(created_by=user_id)

                # Filter by path if provided
                if path:
                    normalized_path = self._validate_path(path)
                    query = query.filter_by(resource_id=normalized_path)

                # Filter out revoked unless requested
                if not include_revoked:
                    query = query.filter(ShareLinkModel.revoked_at.is_(None))

                # Filter out expired unless requested
                if not include_expired:
                    now = datetime.now(UTC)
                    query = query.filter(
                        (ShareLinkModel.expires_at.is_(None)) | (ShareLinkModel.expires_at >= now)
                    )

                # Order by creation date (newest first)
                query = query.order_by(ShareLinkModel.created_at.desc())

                links = query.all()

                result = []
                for link in links:
                    result.append(
                        {
                            "link_id": link.link_id,
                            "path": link.resource_id,
                            "resource_type": link.resource_type,
                            "permission_level": link.permission_level,
                            "created_at": link.created_at.isoformat(),
                            "expires_at": (
                                link.expires_at.isoformat() if link.expires_at else None
                            ),
                            "max_access_count": link.max_access_count,
                            "access_count": link.access_count,
                            "has_password": link.password_hash is not None,
                            "is_valid": link.is_valid(),
                            "revoked_at": (
                                link.revoked_at.isoformat() if link.revoked_at else None
                            ),
                        }
                    )

                return HandlerResponse.ok({"links": result, "count": len(result)})

        except Exception as e:
            return HandlerResponse.error(f"Failed to list share links: {e}", code=500)

    @rpc_expose(description="Revoke a share link")
    def revoke_share_link(
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
        from nexus.storage.models import ShareLinkModel

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

        try:
            with self.SessionLocal() as session:
                link = session.query(ShareLinkModel).filter_by(link_id=link_id).first()

                if not link:
                    return HandlerResponse.error(
                        f"Share link not found: {link_id}", code=404, is_expected=True
                    )

                # Check authorization
                is_owner = link.created_by == user_id and link.zone_id == zone_id
                if not is_owner and not is_admin:
                    return HandlerResponse.error(
                        "Permission denied: only link creator or admin can revoke",
                        code=403,
                        is_expected=True,
                    )

                # Check if already revoked
                if link.revoked_at is not None:
                    return HandlerResponse.error(
                        "Share link is already revoked", code=400, is_expected=True
                    )

                # Revoke the link
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

    @rpc_expose(description="Access a shared resource via share link")
    def access_share_link(
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
        from nexus.storage.models import ShareLinkAccessLogModel, ShareLinkModel

        # Get authenticated user info if available
        accessed_by_user_id = None
        accessed_by_zone_id = None
        if context:
            accessed_by_user_id = getattr(context, "user", None) or getattr(
                context, "subject_id", None
            )
            accessed_by_zone_id = getattr(context, "zone_id", None)

        try:
            with self.SessionLocal() as session:
                link = session.query(ShareLinkModel).filter_by(link_id=link_id).first()

                # Helper to log access attempt
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

                # Check link exists
                if not link:
                    # Don't log for non-existent links (prevents enumeration info)
                    return HandlerResponse.error(
                        "Share link not found or invalid", code=404, is_expected=True
                    )

                # Check revocation
                if link.revoked_at is not None:
                    log_access(False, "revoked")
                    session.commit()
                    return HandlerResponse.error(
                        "Share link has been revoked", code=403, is_expected=True
                    )

                # Check expiration
                now = datetime.now(UTC)
                if link.expires_at is not None and link.expires_at < now:
                    log_access(False, "expired")
                    session.commit()
                    return HandlerResponse.error(
                        "Share link has expired", code=410, is_expected=True
                    )

                # Check access count limit
                if link.max_access_count is not None and link.access_count >= link.max_access_count:
                    log_access(False, "limit_exceeded")
                    session.commit()
                    return HandlerResponse.error(
                        "Share link access limit exceeded",
                        code=429,
                        is_expected=True,
                    )

                # Check password
                if link.password_hash is not None:
                    if not password:
                        log_access(False, "password_required")
                        session.commit()
                        return HandlerResponse.error(
                            "Password required for this share link",
                            code=401,
                            is_expected=True,
                        )
                    if not self._verify_share_link_password(password, link.password_hash):
                        log_access(False, "wrong_password")
                        session.commit()
                        return HandlerResponse.error(
                            "Incorrect password", code=401, is_expected=True
                        )

                # Success - update counters and log
                link.access_count += 1
                link.last_accessed_at = now
                log_access(True)
                session.commit()

                # Return access info
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
                        "expires_at": (link.expires_at.isoformat() if link.expires_at else None),
                    }
                )

        except Exception as e:
            return HandlerResponse.error(f"Failed to access share link: {e}", code=500)

    @rpc_expose(description="Get access logs for a share link")
    def get_share_link_access_logs(
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
        from nexus.storage.models import ShareLinkAccessLogModel, ShareLinkModel

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

        try:
            with self.SessionLocal() as session:
                # First verify the link exists and user has access
                link = session.query(ShareLinkModel).filter_by(link_id=link_id).first()

                if not link:
                    return HandlerResponse.error(
                        f"Share link not found: {link_id}", code=404, is_expected=True
                    )

                # Check authorization
                is_owner = link.created_by == user_id and link.zone_id == zone_id
                if not is_owner and not is_admin:
                    return HandlerResponse.error(
                        "Permission denied: only link creator or admin can view logs",
                        code=403,
                        is_expected=True,
                    )

                # Get logs
                logs = (
                    session.query(ShareLinkAccessLogModel)
                    .filter_by(link_id=link_id)
                    .order_by(ShareLinkAccessLogModel.accessed_at.desc())
                    .limit(limit)
                    .all()
                )

                result = []
                for log in logs:
                    result.append(
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
                    )

                return HandlerResponse.ok({"logs": result, "count": len(result)})

        except Exception as e:
            return HandlerResponse.error(f"Failed to get access logs: {e}", code=500)

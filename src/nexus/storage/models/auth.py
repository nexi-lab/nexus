"""User, API key, OAuth, and zone models.

Issue #1286: Extracted from monolithic __init__.py.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.core.exceptions import ValidationError
from nexus.storage.models._base import Base, uuid_pk

if TYPE_CHECKING:
    from nexus.storage.zone_settings import ZoneSettings


class UserModel(Base):
    """Core user account model.

    Stores user identity and profile information.
    Supports multiple authentication methods and external user management.
    """

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    primary_auth_method: Mapped[str] = mapped_column(String(50), nullable=False, default="password")

    external_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_user_service: Mapped[str | None] = mapped_column(String(100), nullable=True)

    api_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    zone_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    is_global_admin: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    is_active: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    email_verified: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    oauth_accounts: Mapped[list[UserOAuthAccountModel]] = relationship(
        "UserOAuthAccountModel", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_users_email", "email"),
        Index("idx_users_username", "username"),
        Index("idx_users_auth_method", "primary_auth_method"),
        Index("idx_users_external", "external_user_service", "external_user_id"),
        Index("idx_users_active", "is_active"),
        Index("idx_users_deleted", "deleted_at"),
        Index("idx_users_email_active_deleted", "email", "is_active", "deleted_at"),
    )

    def __repr__(self) -> str:
        return f"<UserModel(user_id={self.user_id}, email={self.email}, username={self.username})>"

    def is_deleted(self) -> bool:
        """Check if user is soft deleted."""
        return self.is_active == 0 or self.deleted_at is not None


class UserOAuthAccountModel(Base):
    """OAuth provider accounts linked to users for authentication."""

    __tablename__ = "user_oauth_accounts"

    oauth_account_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    encrypted_id_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    provider_profile: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[UserModel] = relationship("UserModel", back_populates="oauth_accounts")

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_provider_user"),
        Index("idx_user_oauth_user", "user_id"),
        Index("idx_user_oauth_provider", "provider"),
        Index("idx_user_oauth_provider_user", "provider", "provider_user_id"),
    )

    def __repr__(self) -> str:
        return f"<UserOAuthAccountModel(oauth_account_id={self.oauth_account_id}, provider={self.provider}, user_id={self.user_id})>"


class APIKeyModel(Base):
    """Database-backed API key storage with HMAC-SHA256 hashing."""

    __tablename__ = "api_keys"

    key_id: Mapped[str] = uuid_pk()

    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(50), nullable=True, default="user")
    subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)
    is_admin: Mapped[int] = mapped_column(Integer, default=0)

    inherit_permissions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[int] = mapped_column(Integer, default=0, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OAuthAPIKeyModel(Base):
    """Stores encrypted API key values for OAuth users."""

    __tablename__ = "oauth_api_keys"

    key_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("api_keys.key_id", ondelete="CASCADE"),
        primary_key=True,
    )

    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )

    encrypted_key_value: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("idx_oauth_api_keys_user", "user_id"),)

    def __repr__(self) -> str:
        return f"<OAuthAPIKeyModel(key_id={self.key_id}, user_id={self.user_id})>"


class OAuthCredentialModel(Base):
    """OAuth 2.0 credential storage for backend integrations.

    Stores encrypted OAuth tokens for services like Google Drive, Microsoft Graph, etc.
    """

    __tablename__ = "oauth_credentials"

    credential_id: Mapped[str] = uuid_pk()

    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    user_email: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    token_type: Mapped[str] = mapped_column(String(50), nullable=False, default="Bearer")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)

    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Token rotation fields (Issue #997, RFC 9700)
    token_family_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=lambda: str(uuid.uuid4()), index=True
    )
    rotation_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("provider", "user_email", "zone_id", name="uq_oauth_credential"),
        Index("idx_oauth_provider", "provider"),
        Index("idx_oauth_user_email", "user_email"),
        Index("idx_oauth_user_id", "user_id"),
        Index("idx_oauth_zone", "zone_id"),
        Index("idx_oauth_expires", "expires_at"),
        Index("idx_oauth_revoked", "revoked"),
        Index("idx_oauth_token_family", "token_family_id"),
    )

    def __repr__(self) -> str:
        return f"<OAuthCredentialModel(credential_id={self.credential_id}, provider={self.provider}, user_email={self.user_email}, user_id={self.user_id})>"

    def is_expired(self) -> bool:
        """Check if the access token is expired."""
        if self.expires_at is None:
            return False
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return datetime.now(UTC) >= expires_at

    def is_valid(self) -> bool:
        """Check if the credential is valid (not revoked and not expired)."""
        return not self.revoked and not self.is_expired()

    def validate(self) -> None:
        """Validate OAuth credential structural invariants.

        Provider name validation is handled by OAuthProviderFactory at the
        service layer. This method only checks structural invariants.
        """
        if not self.provider:
            raise ValidationError("provider is required")
        if not self.user_email:
            raise ValidationError("user_email is required")
        if not self.encrypted_access_token:
            raise ValidationError("encrypted_access_token is required")
        if self.scopes:
            try:
                scopes_list = json.loads(self.scopes)
                if not isinstance(scopes_list, list):
                    raise ValidationError("scopes must be a JSON array")
            except json.JSONDecodeError as e:
                raise ValidationError(f"scopes must be valid JSON: {e}") from None


class ZoneModel(Base):
    """Zone metadata model.

    Stores organizational/zone information for multi-zone isolation.
    """

    __tablename__ = "zones"

    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    settings: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("idx_zones_name", "name"),
        Index("idx_zones_active", "is_active"),
    )

    @property
    def parsed_settings(self) -> ZoneSettings:
        """Parse settings JSON into a ZoneSettings Pydantic model."""
        from nexus.storage.zone_settings import ZoneSettings

        if self.settings is None:
            return ZoneSettings()
        return ZoneSettings(**json.loads(self.settings))

    def __repr__(self) -> str:
        return (
            f"<ZoneModel(zone_id={self.zone_id}, name={self.name}, "
            f"domain={self.domain}, is_active={self.is_active})>"
        )


class ExternalUserServiceModel(Base):
    """Configuration for external user management services."""

    __tablename__ = "external_user_services"

    service_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    service_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)

    auth_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    user_lookup_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)

    auth_method: Mapped[str] = mapped_column(String(50), nullable=False)

    encrypted_config: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"<ExternalUserServiceModel(service_id={self.service_id}, service_name={self.service_name})>"

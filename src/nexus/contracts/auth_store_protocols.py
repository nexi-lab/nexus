"""Store Protocol interfaces for auth brick decoupling.

These ``@runtime_checkable`` Protocols define the boundary between the
auth brick and the persistence layer.  The auth brick depends only on
these interfaces — never on SQLAlchemy models directly.

Issue #2436: Move auth/ to bricks/auth/ with Protocol DI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from nexus.contracts.auth_store_types import (
    APIKeyDTO,
    OAuthAccountDTO,
    OAuthCredentialDTO,
    SystemSettingDTO,
    UserDTO,
    ZoneDTO,
)


@runtime_checkable
class UserStoreProtocol(Protocol):
    """Session-free user CRUD operations."""

    def create_user(
        self,
        *,
        user_id: str,
        email: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
        password_hash: str | None = None,
        primary_auth_method: str = "password",
        is_admin: bool = False,
        email_verified: bool = False,
        zone_id: str | None = None,
        user_metadata: str | None = None,
    ) -> UserDTO:
        """Create a new user. Returns the created DTO."""
        ...

    def get_by_id(self, user_id: str) -> UserDTO | None:
        """Get active user by user ID."""
        ...

    def get_by_email(self, email: str) -> UserDTO | None:
        """Get active user by email."""
        ...

    def get_by_username(self, username: str) -> UserDTO | None:
        """Get active user by username."""
        ...

    def update_user(self, user_id: str, **fields: object) -> UserDTO | None:
        """Update user fields. Returns updated DTO or None if not found."""
        ...

    def check_email_available(self, email: str) -> bool:
        """Check if email is available for registration."""
        ...

    def check_username_available(self, username: str) -> bool:
        """Check if username is available for registration."""
        ...


@runtime_checkable
class APIKeyStoreProtocol(Protocol):
    """Session-free API key operations."""

    def create_key(
        self,
        *,
        key_hash: str,
        user_id: str,
        name: str,
        subject_type: str = "user",
        subject_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool = False,
        expires_at: datetime | None = None,
        inherit_permissions: bool = False,
    ) -> APIKeyDTO:
        """Create a new API key record. Returns the created DTO."""
        ...

    def get_by_hash(self, key_hash: str) -> APIKeyDTO | None:
        """Get non-revoked API key by hash."""
        ...

    def revoke_key(self, key_id: str, *, zone_id: str | None = None) -> bool:
        """Revoke an API key. Returns True if revoked."""
        ...

    def update_last_used(self, key_hash: str) -> None:
        """Fire-and-forget update of last_used_at timestamp."""
        ...


@runtime_checkable
class OAuthCredentialStoreProtocol(Protocol):
    """Session-free OAuth credential operations."""

    def store_credential(
        self,
        *,
        provider: str,
        user_email: str,
        zone_id: str,
        encrypted_access_token: str,
        encrypted_refresh_token: str | None = None,
        token_type: str = "Bearer",
        expires_at: datetime | None = None,
        scopes: str | None = None,
        client_id: str | None = None,
        token_uri: str | None = None,
        user_id: str | None = None,
        created_by: str | None = None,
        refresh_token_hash: str | None = None,
    ) -> OAuthCredentialDTO:
        """Store or update an OAuth credential. Returns DTO."""
        ...

    def get_credential(
        self, provider: str, user_email: str, zone_id: str
    ) -> OAuthCredentialDTO | None:
        """Get non-revoked credential metadata."""
        ...

    def update_tokens(
        self,
        credential_id: str,
        *,
        encrypted_access_token: str,
        encrypted_refresh_token: str | None = None,
        expires_at: datetime | None = None,
        refresh_token_hash: str | None = None,
        rotation_counter: int | None = None,
        token_family_id: str | None = None,
    ) -> None:
        """Update token fields on an existing credential."""
        ...

    def revoke_credential(self, provider: str, user_email: str, zone_id: str) -> bool:
        """Revoke a credential. Returns True if revoked."""
        ...

    def list_credentials(
        self,
        *,
        zone_id: str | None = None,
        user_email: str | None = None,
        user_id: str | None = None,
    ) -> list[OAuthCredentialDTO]:
        """List non-revoked credential metadata."""
        ...


@runtime_checkable
class OAuthAccountStoreProtocol(Protocol):
    """Session-free OAuth account link operations."""

    def create_account(
        self,
        *,
        user_id: str,
        provider: str,
        provider_user_id: str,
        provider_email: str | None = None,
        display_name: str | None = None,
    ) -> OAuthAccountDTO:
        """Create a new OAuth account link. Returns DTO."""
        ...

    def get_by_provider(self, provider: str, provider_user_id: str) -> OAuthAccountDTO | None:
        """Get account by provider + provider user ID."""
        ...

    def get_accounts_for_user(self, user_id: str) -> list[OAuthAccountDTO]:
        """Get all OAuth accounts linked to a user."""
        ...

    def update_last_used(self, oauth_account_id: str) -> None:
        """Update last_used_at timestamp."""
        ...

    def delete_account(self, oauth_account_id: str) -> bool:
        """Delete an OAuth account link. Returns True if deleted."""
        ...


@runtime_checkable
class ZoneStoreProtocol(Protocol):
    """Session-free zone operations."""

    def create_zone(
        self,
        *,
        zone_id: str,
        name: str,
        domain: str | None = None,
        description: str | None = None,
        settings: str | None = None,
    ) -> ZoneDTO:
        """Create a new zone. Returns DTO."""
        ...

    def get_zone(self, zone_id: str) -> ZoneDTO | None:
        """Get zone by ID."""
        ...

    def zone_exists(self, zone_id: str) -> bool:
        """Check if zone exists."""
        ...


@runtime_checkable
class SystemSettingsStoreProtocol(Protocol):
    """Session-free system settings operations."""

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        """Get a system setting by key."""
        ...

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        """Create or update a system setting."""
        ...


@runtime_checkable
class SessionFactoryProtocol(Protocol):
    """Formalizes the session factory callable pattern."""

    def __call__(self) -> object:
        """Return a new session context manager."""
        ...

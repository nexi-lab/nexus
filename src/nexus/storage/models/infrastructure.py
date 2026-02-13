"""Infrastructure, system config, sandbox, subscription, and migration models.

Issue #1286: Extracted from monolithic __init__.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.exceptions import ValidationError
from nexus.storage.models._base import Base, ResourceConfigMixin, TimestampMixin, uuid_pk


class SandboxMetadataModel(Base):
    """Sandbox metadata for Nexus-managed sandboxes (E2B, etc.)."""

    __tablename__ = "sandbox_metadata"

    sandbox_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="e2b")
    template_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    auto_created: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)

    provider_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("idx_sandbox_name", "name"),
        Index("idx_sandbox_user", "user_id"),
        Index("idx_sandbox_agent", "agent_id"),
        Index("idx_sandbox_zone", "zone_id"),
        Index("idx_sandbox_status", "status"),
        Index("idx_sandbox_expires", "expires_at"),
        Index("idx_sandbox_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<SandboxMetadataModel(sandbox_id={self.sandbox_id}, name={self.name}, user={self.user_id}, status={self.status})>"

    def validate(self) -> None:
        """Validate sandbox metadata before database operations."""
        if not self.sandbox_id:
            raise ValidationError("sandbox_id is required")
        if not self.name:
            raise ValidationError("name is required")
        if not self.user_id:
            raise ValidationError("user_id is required")
        if not self.zone_id:
            raise ValidationError("zone_id is required")
        valid_providers = ["e2b", "docker", "modal"]
        if self.provider not in valid_providers:
            raise ValidationError(f"provider must be one of {valid_providers}, got {self.provider}")
        valid_statuses = ["creating", "active", "paused", "stopping", "stopped", "error"]
        if self.status not in valid_statuses:
            raise ValidationError(f"status must be one of {valid_statuses}, got {self.status}")
        if self.ttl_minutes is not None and self.ttl_minutes < 1:
            raise ValidationError(f"ttl_minutes must be >= 1, got {self.ttl_minutes}")


class MountConfigModel(TimestampMixin, Base):
    """Persistent mount configuration storage.

    Stores backend mount configurations to survive server restarts.
    """

    __tablename__ = "mount_configs"

    mount_id: Mapped[str] = uuid_pk()

    mount_point: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    backend_type: Mapped[str] = mapped_column(String(50), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    readonly: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)

    backend_config: Mapped[str] = mapped_column(Text, nullable=False)

    owner_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    conflict_strategy: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)

    __table_args__ = (
        Index("idx_mount_configs_owner", "owner_user_id"),
        Index("idx_mount_configs_zone", "zone_id"),
        Index("idx_mount_configs_backend_type", "backend_type"),
    )

    def __repr__(self) -> str:
        return f"<MountConfigModel(mount_id={self.mount_id}, mount_point={self.mount_point}, backend_type={self.backend_type})>"

    def validate(self) -> None:
        """Validate mount config model before database operations."""
        if not self.mount_point:
            raise ValidationError("mount_point is required")
        if not self.mount_point.startswith("/"):
            raise ValidationError(f"mount_point must start with '/', got {self.mount_point!r}")
        if not self.backend_type:
            raise ValidationError("backend_type is required")
        if not self.backend_config:
            raise ValidationError("backend_config is required")
        try:
            json.loads(self.backend_config)
        except json.JSONDecodeError as e:
            raise ValidationError(f"backend_config must be valid JSON: {e}") from None
        if self.priority is not None and self.priority < 0:
            raise ValidationError(f"priority must be non-negative, got {self.priority}")


class SystemSettingsModel(TimestampMixin, Base):
    """System-wide settings stored in the database."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)

    value: Mapped[str] = mapped_column(Text, nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_sensitive: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        value_display = "***" if self.is_sensitive else self.value[:50]
        return f"<SystemSettingsModel(key={self.key}, value={value_display})>"


class SubscriptionModel(TimestampMixin, Base):
    """Webhook subscriptions for event notifications."""

    __tablename__ = "subscriptions"

    subscription_id: Mapped[str] = uuid_pk()

    zone_id: Mapped[str] = mapped_column(String(36), nullable=False)

    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str | None] = mapped_column(String(255), nullable=True)

    event_types: Mapped[str] = mapped_column(
        Text, nullable=False, default='["file_write", "file_delete", "file_rename"]'
    )
    patterns: Mapped[str | None] = mapped_column(Text, nullable=True)

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_delivery_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("idx_subscriptions_zone", "zone_id"),
        Index("idx_subscriptions_enabled", "enabled"),
        Index("idx_subscriptions_url", "url"),
    )

    def __repr__(self) -> str:
        return f"<SubscriptionModel(subscription_id={self.subscription_id}, url={self.url[:50]})>"

    def validate(self) -> None:
        """Validate subscription model before database operations."""
        if not self.url:
            raise ValidationError("url is required")
        if not self.url.startswith(("http://", "https://")):
            raise ValidationError("url must be a valid HTTP/HTTPS URL")
        if self.event_types:
            try:
                event_list = json.loads(self.event_types)
                if not isinstance(event_list, list):
                    raise ValidationError("event_types must be a JSON array")
                valid_events = [
                    "file_write",
                    "file_delete",
                    "file_rename",
                    "metadata_change",
                    "dir_create",
                    "dir_delete",
                ]
                for evt in event_list:
                    if evt not in valid_events:
                        raise ValidationError(f"Invalid event type: {evt}")
            except json.JSONDecodeError as e:
                raise ValidationError(f"event_types must be valid JSON: {e}") from e
        if self.patterns:
            try:
                pattern_list = json.loads(self.patterns)
                if not isinstance(pattern_list, list):
                    raise ValidationError("patterns must be a JSON array")
            except json.JSONDecodeError as e:
                raise ValidationError(f"patterns must be valid JSON: {e}") from e

    def get_event_types(self) -> list[str]:
        """Get event types as a Python list."""
        result: list[str] = json.loads(self.event_types) if self.event_types else []
        return result

    def get_patterns(self) -> list[str]:
        """Get patterns as a Python list."""
        result: list[str] = json.loads(self.patterns) if self.patterns else []
        return result

    def get_metadata(self) -> dict[str, Any]:
        """Get custom_metadata as a Python dict."""
        result: dict[str, Any] = json.loads(self.custom_metadata) if self.custom_metadata else {}
        return result


class MigrationHistoryModel(Base):
    """Tracks migration history for upgrade/rollback support."""

    __tablename__ = "migration_history"

    id: Mapped[str] = uuid_pk()

    from_version: Mapped[str] = mapped_column(String(20), nullable=False)
    to_version: Mapped[str] = mapped_column(String(20), nullable=False)

    migration_type: Mapped[str] = mapped_column(String(50), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    backup_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_migration_history_status", "status"),
        Index("idx_migration_history_started_at", "started_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<MigrationHistoryModel(id={self.id}, "
            f"{self.from_version}->{self.to_version}, "
            f"type={self.migration_type}, status={self.status})>"
        )


class WorkspaceConfigModel(ResourceConfigMixin, Base):
    """Workspace configuration registry.

    Tracks which directories are registered as workspaces.
    """

    __tablename__ = "workspace_configs"

    path: Mapped[str] = mapped_column(Text, primary_key=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_workspace_configs_created_at", "created_at"),
        Index("idx_workspace_configs_user", "user_id"),
        Index("idx_workspace_configs_agent", "agent_id"),
        Index("idx_workspace_configs_session", "session_id"),
        Index("idx_workspace_configs_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<WorkspaceConfigModel(path={self.path}, name={self.name})>"


class UserSessionModel(Base):
    """User session tracking for session-scoped resources."""

    __tablename__ = "user_sessions"

    session_id: Mapped[str] = uuid_pk()

    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activity: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_session_user", "user_id"),
        Index("idx_session_agent", "agent_id"),
        Index("idx_session_zone", "zone_id"),
        Index("idx_session_expires", "expires_at"),
        Index("idx_session_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<UserSessionModel(session_id={self.session_id}, user_id={self.user_id}, expires_at={self.expires_at})>"

    def is_expired(self) -> bool:
        """Check if session has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) > self.expires_at

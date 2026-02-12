"""SQLAlchemy models for Nexus metadata store.

All models are organized by domain in submodules (Issue #1286).
This __init__.py re-exports everything for backward compatibility.

Domain modules:
    models._base           -- Base, mixins, uuid_pk
    models.filesystem      -- File storage models
    models.permissions     -- ReBAC and Tiger Cache models
    models.memory          -- Memory and knowledge graph models
    models.auth            -- User, API key, OAuth models
    models.workflows       -- Workflow models
    models.payments        -- Payment and billing models
    models.sharing         -- Share link models
    models.infrastructure  -- System config, sandbox, session models
    models.agents          -- Agent lifecycle models
    models.ace             -- Trajectory and playbook models
    models.sync            -- Sync and conflict models
    models.file_path       -- FilePathModel
    models.version_history -- VersionHistoryModel
    models.operation_log   -- OperationLogModel
    models.audit_checkpoint -- AuditCheckpointModel
    models.exchange_audit_log -- ExchangeAuditLogModel
"""

# Base and mixins
from nexus.storage.models._base import Base as Base
from nexus.storage.models._base import ResourceConfigMixin as ResourceConfigMixin
from nexus.storage.models._base import TimestampMixin as TimestampMixin
from nexus.storage.models._base import ZoneIsolationMixin as ZoneIsolationMixin
from nexus.storage.models._base import _generate_uuid as _generate_uuid
from nexus.storage.models._base import _get_uuid_server_default as _get_uuid_server_default
from nexus.storage.models._base import uuid_pk as uuid_pk

# Domain: ACE (Trajectories, Feedback, Playbooks)
from nexus.storage.models.ace import PlaybookModel as PlaybookModel
from nexus.storage.models.ace import TrajectoryFeedbackModel as TrajectoryFeedbackModel
from nexus.storage.models.ace import TrajectoryModel as TrajectoryModel

# Domain: Agents
from nexus.storage.models.agents import AgentEventModel as AgentEventModel
from nexus.storage.models.agents import AgentRecordModel as AgentRecordModel

# Previously extracted models
from nexus.storage.models.audit_checkpoint import AuditCheckpointModel as AuditCheckpointModel

# Domain: Auth (Users, API Keys, OAuth, Zones)
from nexus.storage.models.auth import APIKeyModel as APIKeyModel
from nexus.storage.models.auth import ExternalUserServiceModel as ExternalUserServiceModel
from nexus.storage.models.auth import OAuthAPIKeyModel as OAuthAPIKeyModel
from nexus.storage.models.auth import OAuthCredentialModel as OAuthCredentialModel
from nexus.storage.models.auth import UserModel as UserModel
from nexus.storage.models.auth import UserOAuthAccountModel as UserOAuthAccountModel
from nexus.storage.models.auth import ZoneModel as ZoneModel
from nexus.storage.models.exchange_audit_log import ExchangeAuditLogModel as ExchangeAuditLogModel
from nexus.storage.models.file_path import FilePathModel as FilePathModel

# Domain: Filesystem
from nexus.storage.models.filesystem import ContentCacheModel as ContentCacheModel
from nexus.storage.models.filesystem import ContentChunkModel as ContentChunkModel
from nexus.storage.models.filesystem import DirectoryEntryModel as DirectoryEntryModel
from nexus.storage.models.filesystem import DocumentChunkModel as DocumentChunkModel
from nexus.storage.models.filesystem import FileMetadataModel as FileMetadataModel
from nexus.storage.models.filesystem import WorkspaceSnapshotModel as WorkspaceSnapshotModel

# Domain: Infrastructure (Sandbox, Config, Sessions, Migrations)
from nexus.storage.models.infrastructure import MigrationHistoryModel as MigrationHistoryModel
from nexus.storage.models.infrastructure import MountConfigModel as MountConfigModel
from nexus.storage.models.infrastructure import SandboxMetadataModel as SandboxMetadataModel
from nexus.storage.models.infrastructure import SubscriptionModel as SubscriptionModel
from nexus.storage.models.infrastructure import SystemSettingsModel as SystemSettingsModel
from nexus.storage.models.infrastructure import UserSessionModel as UserSessionModel
from nexus.storage.models.infrastructure import WorkspaceConfigModel as WorkspaceConfigModel

# Domain: Memory and Knowledge Graph
from nexus.storage.models.memory import EntityMentionModel as EntityMentionModel
from nexus.storage.models.memory import EntityModel as EntityModel
from nexus.storage.models.memory import EntityRegistryModel as EntityRegistryModel
from nexus.storage.models.memory import MemoryConfigModel as MemoryConfigModel
from nexus.storage.models.memory import MemoryModel as MemoryModel
from nexus.storage.models.memory import RelationshipModel as RelationshipModel
from nexus.storage.models.operation_log import OperationLogModel as OperationLogModel

# Domain: Payments
from nexus.storage.models.payments import AgentWalletMeta as AgentWalletMeta
from nexus.storage.models.payments import CreditReservationMeta as CreditReservationMeta
from nexus.storage.models.payments import PaymentTransactionMeta as PaymentTransactionMeta
from nexus.storage.models.payments import UsageEvent as UsageEvent

# Domain: Permissions (ReBAC + Tiger Cache)
from nexus.storage.models.permissions import (
    FileSystemVersionSequenceModel as FileSystemVersionSequenceModel,
)
from nexus.storage.models.permissions import ReBACChangelogModel as ReBACChangelogModel
from nexus.storage.models.permissions import ReBACCheckCacheModel as ReBACCheckCacheModel
from nexus.storage.models.permissions import ReBACGroupClosureModel as ReBACGroupClosureModel
from nexus.storage.models.permissions import ReBACNamespaceModel as ReBACNamespaceModel
from nexus.storage.models.permissions import ReBACTupleModel as ReBACTupleModel
from nexus.storage.models.permissions import ReBACVersionSequenceModel as ReBACVersionSequenceModel
from nexus.storage.models.permissions import TigerCacheModel as TigerCacheModel
from nexus.storage.models.permissions import TigerCacheQueueModel as TigerCacheQueueModel
from nexus.storage.models.permissions import TigerDirectoryGrantsModel as TigerDirectoryGrantsModel
from nexus.storage.models.permissions import TigerResourceMapModel as TigerResourceMapModel

# Domain: Sharing
from nexus.storage.models.sharing import ShareLinkAccessLogModel as ShareLinkAccessLogModel
from nexus.storage.models.sharing import ShareLinkModel as ShareLinkModel

# Domain: Sync and Conflict Resolution
from nexus.storage.models.sync import BackendChangeLogModel as BackendChangeLogModel
from nexus.storage.models.sync import ConflictLogModel as ConflictLogModel
from nexus.storage.models.sync import SyncBacklogModel as SyncBacklogModel
from nexus.storage.models.sync import SyncJobModel as SyncJobModel
from nexus.storage.models.version_history import VersionHistoryModel as VersionHistoryModel

# Domain: Workflows
from nexus.storage.models.workflows import WorkflowExecutionModel as WorkflowExecutionModel
from nexus.storage.models.workflows import WorkflowModel as WorkflowModel

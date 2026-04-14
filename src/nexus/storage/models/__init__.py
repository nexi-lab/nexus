"""SQLAlchemy models for Nexus metadata store.

All models are organized by domain in submodules (Issue #1286).
This __init__.py re-exports all models for convenient access.

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
    models.file_path       -- FilePathModel
    models.version_history -- VersionHistoryModel
    models.operation_log   -- OperationLogModel
    models.audit_checkpoint -- AuditCheckpointModel
    models.exchange_audit_log -- ExchangeAuditLogModel
    models.identity        -- AgentKeyModel (Issue #1355)
    models.transaction_snapshot -- TransactionSnapshotModel, SnapshotEntryModel (Issue #1752)
"""

# Base and mixins
from nexus.storage.models._base import Base as Base
from nexus.storage.models._base import ResourceConfigMixin as ResourceConfigMixin
from nexus.storage.models._base import TimestampMixin as TimestampMixin
from nexus.storage.models._base import ZoneIsolationMixin as ZoneIsolationMixin
from nexus.storage.models._base import _generate_uuid as _generate_uuid
from nexus.storage.models._base import _get_uuid_server_default as _get_uuid_server_default
from nexus.storage.models._base import uuid_pk as uuid_pk

# Domain: Access Manifests (Issue #1754)
from nexus.storage.models.access_manifest import AccessManifestModel as AccessManifestModel

# Domain: Agents
from nexus.storage.models.agents import AgentEventModel as AgentEventModel
from nexus.storage.models.agents import DelegationRecordModel as DelegationRecordModel

# Domain: Knowledge Platform (Issue #2929)
from nexus.storage.models.aspect_store import EntityAspectModel as EntityAspectModel

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

# Domain: Context Branching (Issue #1315)
from nexus.storage.models.context_branch import ContextBranchModel as ContextBranchModel
from nexus.storage.models.dead_letter import DeadLetterModel as DeadLetterModel
from nexus.storage.models.exchange_audit_log import ExchangeAuditLogModel as ExchangeAuditLogModel
from nexus.storage.models.file_path import FilePathModel as FilePathModel

# Domain: Filesystem
from nexus.storage.models.filesystem import DirectoryEntryModel as DirectoryEntryModel
from nexus.storage.models.filesystem import DocumentChunkModel as DocumentChunkModel
from nexus.storage.models.filesystem import FileMetadataModel as FileMetadataModel
from nexus.storage.models.filesystem import WorkspaceSnapshotModel as WorkspaceSnapshotModel

# Domain: Identity (Agent signing keys, Issue #1355; Credentials, Issue #1753)
from nexus.storage.models.identity import AgentCredentialModel as AgentCredentialModel
from nexus.storage.models.identity import AgentKeyModel as AgentKeyModel

# Domain: Infrastructure (Sandbox, Config, Sessions, Migrations, Settings)
from nexus.storage.models.infrastructure import MigrationHistoryModel as MigrationHistoryModel
from nexus.storage.models.infrastructure import SandboxMetadataModel as SandboxMetadataModel
from nexus.storage.models.infrastructure import SubscriptionModel as SubscriptionModel
from nexus.storage.models.infrastructure import SystemSettingsModel as SystemSettingsModel

# Domain: Lineage Tracking (Issue #3417)
from nexus.storage.models.lineage_reverse_index import (
    LineageReverseIndexModel as LineageReverseIndexModel,
)

# Domain: Memory and Knowledge Graph
from nexus.storage.models.memory import EntityMentionModel as EntityMentionModel
from nexus.storage.models.memory import EntityModel as EntityModel
from nexus.storage.models.memory import EntityRegistryModel as EntityRegistryModel
from nexus.storage.models.memory import MemoryModel as MemoryModel
from nexus.storage.models.memory import RelationshipModel as RelationshipModel
from nexus.storage.models.metadata_change_log import (
    MetadataChangeLogModel as MetadataChangeLogModel,
)
from nexus.storage.models.operation_log import OperationLogModel as OperationLogModel

# Domain: Path Registration (Issue #189 — merged WorkspaceConfig + MemoryConfig)
from nexus.storage.models.path_registration import PathRegistrationModel as PathRegistrationModel

# Domain: Payments
from nexus.storage.models.payments import AgentWalletMeta as AgentWalletMeta
from nexus.storage.models.payments import CreditReservationMeta as CreditReservationMeta
from nexus.storage.models.payments import PaymentTransactionMeta as PaymentTransactionMeta
from nexus.storage.models.payments import UsageEvent as UsageEvent

# Domain: Permissions (ReBAC + Tiger Cache)
from nexus.storage.models.permissions import AdminBypassAuditModel as AdminBypassAuditModel
from nexus.storage.models.permissions import ReBACChangelogModel as ReBACChangelogModel
from nexus.storage.models.permissions import ReBACGroupClosureModel as ReBACGroupClosureModel
from nexus.storage.models.permissions import ReBACTupleModel as ReBACTupleModel
from nexus.storage.models.permissions import ReBACVersionSequenceModel as ReBACVersionSequenceModel
from nexus.storage.models.permissions import TigerCacheModel as TigerCacheModel
from nexus.storage.models.permissions import TigerCacheQueueModel as TigerCacheQueueModel
from nexus.storage.models.permissions import TigerDirectoryGrantsModel as TigerDirectoryGrantsModel
from nexus.storage.models.permissions import TigerResourceMapModel as TigerResourceMapModel

# Domain: OAuth Token Rotation (Issue #997)
from nexus.storage.models.refresh_token_history import (
    RefreshTokenHistoryModel as RefreshTokenHistoryModel,
)

# Domain: Scheduler (Task Queue, Issue #1212)
from nexus.storage.models.scheduler import ScheduledTaskModel as ScheduledTaskModel

# Domain: Secrets Store
from nexus.storage.models.secret_store import SecretStoreModel as SecretStoreModel
from nexus.storage.models.secret_store import SecretStoreVersionModel as SecretStoreVersionModel

# Domain: Secrets Audit (Issue #997)
from nexus.storage.models.secrets_audit_log import SecretsAuditEventType as SecretsAuditEventType
from nexus.storage.models.secrets_audit_log import SecretsAuditLogModel as SecretsAuditLogModel

# Domain: Sharing
from nexus.storage.models.sharing import ShareLinkAccessLogModel as ShareLinkAccessLogModel
from nexus.storage.models.sharing import ShareLinkModel as ShareLinkModel

# Domain: Spending Policy (Issue #1358)
from nexus.storage.models.spending_policy import SpendingApprovalModel as SpendingApprovalModel
from nexus.storage.models.spending_policy import SpendingLedgerModel as SpendingLedgerModel
from nexus.storage.models.spending_policy import SpendingPolicyModel as SpendingPolicyModel

# Domain: Transaction Snapshots (Issue #1752)
from nexus.storage.models.transaction_snapshot import SnapshotEntryModel as SnapshotEntryModel
from nexus.storage.models.transaction_snapshot import (
    TransactionSnapshotModel as TransactionSnapshotModel,
)

# Domain: Uploads (Issue #788)
from nexus.storage.models.upload_session import UploadSessionModel as UploadSessionModel
from nexus.storage.models.version_history import VersionHistoryModel as VersionHistoryModel

# Domain: Workflows
from nexus.storage.models.workflows import WorkflowExecutionModel as WorkflowExecutionModel
from nexus.storage.models.workflows import WorkflowModel as WorkflowModel

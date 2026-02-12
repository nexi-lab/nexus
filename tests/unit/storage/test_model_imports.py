"""Smoke test verifying all models are re-exported from nexus.storage.models.

Issue #1286: Ensures backward compatibility after domain file split.
"""

import pytest

from nexus.storage.models._base import Base

# All model classes that must be importable from nexus.storage.models
EXPECTED_MODELS = [
    # Previously extracted
    "FilePathModel",
    "VersionHistoryModel",
    "OperationLogModel",
    "AuditCheckpointModel",
    "ExchangeAuditLogModel",
    # Filesystem
    "DirectoryEntryModel",
    "FileMetadataModel",
    "ContentChunkModel",
    "WorkspaceSnapshotModel",
    "DocumentChunkModel",
    "ContentCacheModel",
    # Permissions
    "ReBACTupleModel",
    "ReBACNamespaceModel",
    "ReBACGroupClosureModel",
    "ReBACChangelogModel",
    "ReBACVersionSequenceModel",
    "FileSystemVersionSequenceModel",
    "ReBACCheckCacheModel",
    "TigerResourceMapModel",
    "TigerCacheModel",
    "TigerCacheQueueModel",
    "TigerDirectoryGrantsModel",
    # Memory
    "MemoryModel",
    "MemoryConfigModel",
    "EntityRegistryModel",
    "EntityModel",
    "RelationshipModel",
    "EntityMentionModel",
    # Auth
    "UserModel",
    "UserOAuthAccountModel",
    "APIKeyModel",
    "OAuthAPIKeyModel",
    "OAuthCredentialModel",
    "ZoneModel",
    "ExternalUserServiceModel",
    # Workflows
    "WorkflowModel",
    "WorkflowExecutionModel",
    # Payments
    "AgentWalletMeta",
    "PaymentTransactionMeta",
    "CreditReservationMeta",
    "UsageEvent",
    # Sharing
    "ShareLinkModel",
    "ShareLinkAccessLogModel",
    # Infrastructure
    "SandboxMetadataModel",
    "MountConfigModel",
    "SystemSettingsModel",
    "SubscriptionModel",
    "MigrationHistoryModel",
    "WorkspaceConfigModel",
    "UserSessionModel",
    # Agents
    "AgentRecordModel",
    "AgentEventModel",
    # ACE
    "TrajectoryModel",
    "TrajectoryFeedbackModel",
    "PlaybookModel",
    # Sync
    "SyncJobModel",
    "BackendChangeLogModel",
    "SyncBacklogModel",
    "ConflictLogModel",
]


class TestModelReExports:
    """Verify all models are importable from nexus.storage.models."""

    @pytest.mark.parametrize("model_name", EXPECTED_MODELS)
    def test_model_importable(self, model_name: str) -> None:
        """Each model should be importable from nexus.storage.models."""
        import nexus.storage.models as models_pkg

        assert hasattr(models_pkg, model_name), (
            f"{model_name} not re-exported from nexus.storage.models"
        )
        cls = getattr(models_pkg, model_name)
        assert isinstance(cls, type), f"{model_name} is not a class"

    def test_base_re_exported(self) -> None:
        """Base should be importable from nexus.storage.models."""
        from nexus.storage.models import Base as ReExportedBase

        assert ReExportedBase is Base

    def test_mixins_re_exported(self) -> None:
        """Mixins and helpers should be re-exported."""
        from nexus.storage.models import (
            ResourceConfigMixin,
            TimestampMixin,
            ZoneIsolationMixin,
            _generate_uuid,
            _get_uuid_server_default,
            uuid_pk,
        )

        assert callable(_generate_uuid)
        assert callable(_get_uuid_server_default)
        assert callable(uuid_pk)
        assert isinstance(TimestampMixin, type)
        assert isinstance(ZoneIsolationMixin, type)
        assert isinstance(ResourceConfigMixin, type)

    def test_all_subclasses_re_exported(self) -> None:
        """Every Base subclass should be re-exported from __init__.py."""
        import nexus.storage.models as models_pkg

        # Force all domain modules to be imported (they're imported by __init__)
        _ = models_pkg.Base

        registered_names = {cls.__name__ for cls in Base.__subclasses__()}
        exported_names = set(EXPECTED_MODELS)

        missing = registered_names - exported_names
        assert not missing, (
            f"These Base subclasses are NOT in EXPECTED_MODELS: {missing}. "
            "Update EXPECTED_MODELS or the re-export manifest."
        )

    def test_no_duplicate_tablenames(self) -> None:
        """Each __tablename__ should be unique across all models."""
        import nexus.storage.models as models_pkg

        _ = models_pkg.Base  # force imports

        tablenames: dict[str, str] = {}
        for cls in Base.__subclasses__():
            tname = getattr(cls, "__tablename__", None)
            if tname is not None:
                assert tname not in tablenames, (
                    f"Duplicate __tablename__ '{tname}' in {cls.__name__} and {tablenames[tname]}"
                )
                tablenames[tname] = cls.__name__

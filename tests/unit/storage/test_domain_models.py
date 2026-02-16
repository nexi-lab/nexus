"""Business logic tests for domain models.

Issue #1286: Tests for model methods (validate, is_valid, is_expired, etc.).
"""

from datetime import UTC, datetime, timedelta

import pytest


class TestMemoryModelValidate:
    """Tests for MemoryModel.validate()."""

    def test_valid_memory(self) -> None:
        from nexus.storage.models.memory import MemoryModel

        m = MemoryModel(content_hash="a" * 64, scope="agent", visibility="private", state="active")
        m.validate()  # should not raise

    def test_missing_content_hash(self) -> None:
        from nexus.storage.models.memory import MemoryModel

        m = MemoryModel(content_hash="", scope="agent", visibility="private", state="active")
        with pytest.raises(Exception, match="content_hash is required"):
            m.validate()

    def test_invalid_scope(self) -> None:
        from nexus.storage.models.memory import MemoryModel

        m = MemoryModel(
            content_hash="a" * 64, scope="invalid", visibility="private", state="active"
        )
        with pytest.raises(Exception, match="scope must be one of"):
            m.validate()

    def test_invalid_visibility(self) -> None:
        from nexus.storage.models.memory import MemoryModel

        m = MemoryModel(content_hash="a" * 64, scope="agent", visibility="invalid", state="active")
        with pytest.raises(Exception, match="visibility must be one of"):
            m.validate()

    def test_invalid_state(self) -> None:
        from nexus.storage.models.memory import MemoryModel

        m = MemoryModel(content_hash="a" * 64, scope="agent", visibility="private", state="xyz")
        with pytest.raises(Exception, match="state must be one of"):
            m.validate()

    def test_importance_out_of_range(self) -> None:
        from nexus.storage.models.memory import MemoryModel

        m = MemoryModel(
            content_hash="a" * 64,
            scope="agent",
            visibility="private",
            state="active",
            importance=1.5,
        )
        with pytest.raises(Exception, match="importance must be between"):
            m.validate()

    def test_importance_none_ok(self) -> None:
        from nexus.storage.models.memory import MemoryModel

        m = MemoryModel(
            content_hash="a" * 64,
            scope="agent",
            visibility="private",
            state="active",
            importance=None,
        )
        m.validate()  # should not raise


class TestUserModelIsDeleted:
    """Tests for UserModel.is_deleted()."""

    def test_active_user(self) -> None:
        from nexus.storage.models.auth import UserModel

        u = UserModel(user_id="u1", is_active=1, deleted_at=None)
        assert u.is_deleted() is False

    def test_inactive_user(self) -> None:
        from nexus.storage.models.auth import UserModel

        u = UserModel(user_id="u1", is_active=0, deleted_at=None)
        assert u.is_deleted() is True

    def test_deleted_at_set(self) -> None:
        from nexus.storage.models.auth import UserModel

        u = UserModel(user_id="u1", is_active=1, deleted_at=datetime.now(UTC))
        assert u.is_deleted() is True


class TestShareLinkModelIsValid:
    """Tests for ShareLinkModel.is_valid()."""

    def test_valid_link(self) -> None:
        from nexus.storage.models.sharing import ShareLinkModel

        link = ShareLinkModel(
            resource_type="file",
            resource_id="f1",
            created_by="u1",
            revoked_at=None,
            expires_at=None,
            max_access_count=None,
            access_count=0,
        )
        assert link.is_valid() is True

    def test_revoked_link(self) -> None:
        from nexus.storage.models.sharing import ShareLinkModel

        link = ShareLinkModel(
            resource_type="file",
            resource_id="f1",
            created_by="u1",
            revoked_at=datetime.now(UTC),
        )
        assert link.is_valid() is False

    def test_expired_link(self) -> None:
        from nexus.storage.models.sharing import ShareLinkModel

        link = ShareLinkModel(
            resource_type="file",
            resource_id="f1",
            created_by="u1",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert link.is_valid() is False

    def test_access_count_exceeded(self) -> None:
        from nexus.storage.models.sharing import ShareLinkModel

        link = ShareLinkModel(
            resource_type="file",
            resource_id="f1",
            created_by="u1",
            max_access_count=5,
            access_count=5,
        )
        assert link.is_valid() is False

    def test_access_count_within_limit(self) -> None:
        from nexus.storage.models.sharing import ShareLinkModel

        link = ShareLinkModel(
            resource_type="file",
            resource_id="f1",
            created_by="u1",
            max_access_count=5,
            access_count=4,
        )
        assert link.is_valid() is True


class TestOAuthCredentialModel:
    """Tests for OAuthCredentialModel business methods."""

    def test_is_expired_none(self) -> None:
        from nexus.storage.models.auth import OAuthCredentialModel

        cred = OAuthCredentialModel(
            provider="google",
            user_email="a@b.com",
            encrypted_access_token="enc",
            expires_at=None,
        )
        assert cred.is_expired() is False

    def test_is_expired_future(self) -> None:
        from nexus.storage.models.auth import OAuthCredentialModel

        cred = OAuthCredentialModel(
            provider="google",
            user_email="a@b.com",
            encrypted_access_token="enc",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert cred.is_expired() is False

    def test_is_expired_past(self) -> None:
        from nexus.storage.models.auth import OAuthCredentialModel

        cred = OAuthCredentialModel(
            provider="google",
            user_email="a@b.com",
            encrypted_access_token="enc",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert cred.is_expired() is True

    def test_is_expired_naive_datetime(self) -> None:
        """Naive datetimes should still work (gets UTC tzinfo added)."""
        from nexus.storage.models.auth import OAuthCredentialModel

        cred = OAuthCredentialModel(
            provider="google",
            user_email="a@b.com",
            encrypted_access_token="enc",
            expires_at=datetime(2000, 1, 1),  # naive, in the past
        )
        assert cred.is_expired() is True

    def test_is_valid_not_revoked(self) -> None:
        from nexus.storage.models.auth import OAuthCredentialModel

        cred = OAuthCredentialModel(
            provider="google",
            user_email="a@b.com",
            encrypted_access_token="enc",
            revoked=0,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert cred.is_valid() is True

    def test_is_valid_revoked(self) -> None:
        from nexus.storage.models.auth import OAuthCredentialModel

        cred = OAuthCredentialModel(
            provider="google",
            user_email="a@b.com",
            encrypted_access_token="enc",
            revoked=1,
        )
        assert cred.is_valid() is False

    def test_validate_unknown_provider_accepted(self) -> None:
        """Provider name validation moved to OAuthProviderFactory (Issue #997)."""
        from nexus.storage.models.auth import OAuthCredentialModel

        cred = OAuthCredentialModel(
            provider="unknown_provider",
            user_email="a@b.com",
            encrypted_access_token="enc",
        )
        cred.validate()  # should not raise — provider validation is at service layer

    def test_validate_invalid_scopes(self) -> None:
        from nexus.storage.models.auth import OAuthCredentialModel

        cred = OAuthCredentialModel(
            provider="google",
            user_email="a@b.com",
            encrypted_access_token="enc",
            scopes="not json",
        )
        with pytest.raises(Exception, match="scopes must be valid JSON"):
            cred.validate()


class TestUserSessionModelIsExpired:
    """Tests for UserSessionModel.is_expired()."""

    def test_persistent_session(self) -> None:
        from nexus.storage.models.infrastructure import UserSessionModel

        s = UserSessionModel(user_id="u1", expires_at=None)
        assert s.is_expired() is False

    def test_unexpired_session(self) -> None:
        from nexus.storage.models.infrastructure import UserSessionModel

        s = UserSessionModel(user_id="u1", expires_at=datetime.now(UTC) + timedelta(hours=1))
        assert s.is_expired() is False

    def test_expired_session(self) -> None:
        from nexus.storage.models.infrastructure import UserSessionModel

        s = UserSessionModel(user_id="u1", expires_at=datetime.now(UTC) - timedelta(hours=1))
        assert s.is_expired() is True


class TestSyncJobModelToDict:
    """Tests for SyncJobModel.to_dict()."""

    def test_to_dict_minimal(self) -> None:
        from nexus.storage.models.sync import SyncJobModel

        job = SyncJobModel(
            id="j1",
            mount_point="/mnt/test",
            status="completed",
            progress_pct=100,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        d = job.to_dict()
        assert d["id"] == "j1"
        assert d["status"] == "completed"
        assert d["progress_pct"] == 100
        assert d["progress_detail"] is None
        assert d["result"] is None

    def test_to_dict_with_json_fields(self) -> None:
        from nexus.storage.models.sync import SyncJobModel

        job = SyncJobModel(
            id="j2",
            mount_point="/mnt/test",
            status="running",
            progress_pct=50,
            progress_detail='{"files_scanned": 50}',
            sync_params='{"path": "/inbox"}',
            result='{"files_created": 10}',
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        d = job.to_dict()
        assert d["progress_detail"] == {"files_scanned": 50}
        assert d["sync_params"] == {"path": "/inbox"}
        assert d["result"] == {"files_created": 10}


class TestSubscriptionModelMethods:
    """Tests for SubscriptionModel helper methods."""

    def test_get_event_types(self) -> None:
        from nexus.storage.models.infrastructure import SubscriptionModel

        sub = SubscriptionModel(
            zone_id="z1",
            url="https://example.com/webhook",
            event_types='["file_write", "file_delete"]',
        )
        assert sub.get_event_types() == ["file_write", "file_delete"]

    def test_get_patterns(self) -> None:
        from nexus.storage.models.infrastructure import SubscriptionModel

        sub = SubscriptionModel(
            zone_id="z1",
            url="https://example.com/webhook",
            patterns='["*.txt", "docs/*"]',
        )
        assert sub.get_patterns() == ["*.txt", "docs/*"]

    def test_get_metadata(self) -> None:
        from nexus.storage.models.infrastructure import SubscriptionModel

        sub = SubscriptionModel(
            zone_id="z1",
            url="https://example.com/webhook",
            custom_metadata='{"team": "eng"}',
        )
        assert sub.get_metadata() == {"team": "eng"}

    def test_get_patterns_none(self) -> None:
        from nexus.storage.models.infrastructure import SubscriptionModel

        sub = SubscriptionModel(
            zone_id="z1",
            url="https://example.com/webhook",
            patterns=None,
        )
        assert sub.get_patterns() == []


class TestUsageEventMetadata:
    """Tests for UsageEvent.get_metadata() / set_metadata()."""

    def test_get_metadata(self) -> None:
        from nexus.storage.models.payments import UsageEvent

        evt = UsageEvent(
            agent_id="a1",
            event_type="api_call",
            amount=1,
            metadata_json='{"model": "gpt-4"}',
        )
        assert evt.get_metadata() == {"model": "gpt-4"}

    def test_get_metadata_none(self) -> None:
        from nexus.storage.models.payments import UsageEvent

        evt = UsageEvent(agent_id="a1", event_type="api_call", amount=1)
        assert evt.get_metadata() == {}

    def test_set_metadata(self) -> None:
        from nexus.storage.models.payments import UsageEvent

        evt = UsageEvent(agent_id="a1", event_type="api_call", amount=1)
        evt.set_metadata({"model": "claude"})
        assert evt.metadata_json == '{"model": "claude"}'

    def test_set_metadata_none(self) -> None:
        from nexus.storage.models.payments import UsageEvent

        evt = UsageEvent(agent_id="a1", event_type="api_call", amount=1)
        evt.set_metadata({})
        assert evt.metadata_json is None


class TestZoneModelParsedSettings:
    """Tests for ZoneModel.parsed_settings."""

    def test_parsed_settings_none(self) -> None:
        from nexus.storage.models.auth import ZoneModel

        z = ZoneModel(zone_id="z1", name="Test")
        settings = z.parsed_settings
        assert settings is not None  # returns default ZoneSettings

    def test_parsed_settings_json(self) -> None:
        from nexus.storage.models.auth import ZoneModel

        z = ZoneModel(zone_id="z1", name="Test", settings="{}")
        settings = z.parsed_settings
        assert settings is not None


class TestTrajectoryModelValidate:
    """Tests for TrajectoryModel.validate()."""

    def test_valid_trajectory(self) -> None:
        from nexus.storage.models.ace import TrajectoryModel

        t = TrajectoryModel(
            user_id="u1",
            task_description="Test task",
            trace_hash="a" * 64,
            status="success",
        )
        t.validate()

    def test_missing_user_id(self) -> None:
        from nexus.storage.models.ace import TrajectoryModel

        t = TrajectoryModel(
            user_id="",
            task_description="Test",
            trace_hash="a" * 64,
            status="success",
        )
        with pytest.raises(Exception, match="user_id is required"):
            t.validate()

    def test_invalid_status(self) -> None:
        from nexus.storage.models.ace import TrajectoryModel

        t = TrajectoryModel(
            user_id="u1",
            task_description="Test",
            trace_hash="a" * 64,
            status="invalid",
        )
        with pytest.raises(Exception, match="status must be one of"):
            t.validate()

    def test_invalid_success_score(self) -> None:
        from nexus.storage.models.ace import TrajectoryModel

        t = TrajectoryModel(
            user_id="u1",
            task_description="Test",
            trace_hash="a" * 64,
            status="success",
            success_score=2.0,
        )
        with pytest.raises(Exception, match="success_score must be between"):
            t.validate()


class TestPlaybookModelValidate:
    """Tests for PlaybookModel.validate()."""

    def test_valid_playbook(self) -> None:
        from nexus.storage.models.ace import PlaybookModel

        p = PlaybookModel(
            user_id="u1",
            name="Test",
            content_hash="a" * 64,
            scope="agent",
            visibility="private",
            success_rate=0.5,
            usage_count=0,
        )
        p.validate()

    def test_invalid_scope(self) -> None:
        from nexus.storage.models.ace import PlaybookModel

        p = PlaybookModel(
            user_id="u1",
            name="Test",
            content_hash="a" * 64,
            scope="bad",
            visibility="private",
            success_rate=0.5,
            usage_count=0,
        )
        with pytest.raises(Exception, match="scope must be one of"):
            p.validate()

    def test_invalid_version(self) -> None:
        from nexus.storage.models.ace import PlaybookModel

        p = PlaybookModel(
            user_id="u1",
            name="Test",
            content_hash="a" * 64,
            scope="agent",
            visibility="private",
            success_rate=0.5,
            usage_count=0,
            version=0,
        )
        with pytest.raises(Exception, match="version must be >= 1"):
            p.validate()


class TestDirectoryEntryModelValidate:
    """Tests for DirectoryEntryModel.validate()."""

    def test_valid_entry(self) -> None:
        from nexus.storage.models.filesystem import DirectoryEntryModel

        e = DirectoryEntryModel(parent_path="/test/", entry_name="file.txt", entry_type="file")
        e.validate()

    def test_missing_slash_prefix(self) -> None:
        from nexus.storage.models.filesystem import DirectoryEntryModel

        e = DirectoryEntryModel(parent_path="test/", entry_name="file.txt", entry_type="file")
        with pytest.raises(Exception, match="parent_path must start with '/'"):
            e.validate()

    def test_missing_slash_suffix(self) -> None:
        from nexus.storage.models.filesystem import DirectoryEntryModel

        e = DirectoryEntryModel(parent_path="/test", entry_name="file.txt", entry_type="file")
        with pytest.raises(Exception, match="parent_path must end with '/'"):
            e.validate()

    def test_invalid_entry_type(self) -> None:
        from nexus.storage.models.filesystem import DirectoryEntryModel

        e = DirectoryEntryModel(parent_path="/test/", entry_name="file.txt", entry_type="symlink")
        with pytest.raises(Exception, match="entry_type must be"):
            e.validate()


class TestContentChunkModelValidate:
    """Tests for ContentChunkModel.validate()."""

    def test_valid_chunk(self) -> None:
        from nexus.storage.models.filesystem import ContentChunkModel

        c = ContentChunkModel(
            content_hash="a" * 64,
            size_bytes=100,
            storage_path="/data/chunks/a",
        )
        c.validate()

    def test_invalid_hash_length(self) -> None:
        from nexus.storage.models.filesystem import ContentChunkModel

        c = ContentChunkModel(content_hash="abc", size_bytes=100, storage_path="/data/chunks/a")
        with pytest.raises(Exception, match="content_hash must be 64 characters"):
            c.validate()

    def test_invalid_hash_chars(self) -> None:
        from nexus.storage.models.filesystem import ContentChunkModel

        c = ContentChunkModel(content_hash="z" * 64, size_bytes=100, storage_path="/data/chunks/a")
        with pytest.raises(Exception, match="hexadecimal"):
            c.validate()

    def test_negative_size(self) -> None:
        from nexus.storage.models.filesystem import ContentChunkModel

        c = ContentChunkModel(content_hash="a" * 64, size_bytes=-1, storage_path="/data/chunks/a")
        with pytest.raises(Exception, match="size_bytes cannot be negative"):
            c.validate()


class TestEntityRegistryModelValidate:
    """Tests for EntityRegistryModel.validate()."""

    def test_valid_entity(self) -> None:
        from nexus.storage.models.memory import EntityRegistryModel

        e = EntityRegistryModel(entity_type="user", entity_id="u1")
        e.validate()

    def test_invalid_type(self) -> None:
        from nexus.storage.models.memory import EntityRegistryModel

        e = EntityRegistryModel(entity_type="invalid", entity_id="u1")
        with pytest.raises(Exception, match="entity_type must be one of"):
            e.validate()

    def test_parent_consistency(self) -> None:
        from nexus.storage.models.memory import EntityRegistryModel

        e = EntityRegistryModel(
            entity_type="user", entity_id="u1", parent_type="zone", parent_id=None
        )
        with pytest.raises(Exception, match="parent_type and parent_id must both"):
            e.validate()


class TestMountConfigModelValidate:
    """Tests for MountConfigModel.validate()."""

    def test_valid_config(self) -> None:
        from nexus.storage.models.infrastructure import MountConfigModel

        m = MountConfigModel(
            mount_point="/mnt/test",
            backend_type="local",
            backend_config="{}",
        )
        m.validate()

    def test_invalid_mount_point(self) -> None:
        from nexus.storage.models.infrastructure import MountConfigModel

        m = MountConfigModel(
            mount_point="mnt/test",
            backend_type="local",
            backend_config="{}",
        )
        with pytest.raises(Exception, match="mount_point must start with '/'"):
            m.validate()

    def test_invalid_json(self) -> None:
        from nexus.storage.models.infrastructure import MountConfigModel

        m = MountConfigModel(
            mount_point="/mnt/test",
            backend_type="local",
            backend_config="not json",
        )
        with pytest.raises(Exception, match="backend_config must be valid JSON"):
            m.validate()


class TestWorkflowModelValidate:
    """Tests for WorkflowModel.validate()."""

    def test_valid_workflow(self) -> None:
        from nexus.storage.models.workflows import WorkflowModel

        w = WorkflowModel(name="test", definition="yaml content", definition_hash="a" * 64)
        w.validate()

    def test_missing_name(self) -> None:
        from nexus.storage.models.workflows import WorkflowModel

        w = WorkflowModel(name="", definition="yaml", definition_hash="a" * 64)
        with pytest.raises(Exception, match="name is required"):
            w.validate()


class TestBackendChangeLogValidate:
    """Tests for BackendChangeLogModel.validate()."""

    def test_valid_log(self) -> None:
        from nexus.storage.models.sync import BackendChangeLogModel

        log = BackendChangeLogModel(path="/test.txt", backend_name="gcs")
        log.validate()

    def test_negative_size(self) -> None:
        from nexus.storage.models.sync import BackendChangeLogModel

        log = BackendChangeLogModel(path="/test.txt", backend_name="gcs", size_bytes=-1)
        with pytest.raises(Exception, match="size_bytes cannot be negative"):
            log.validate()


class TestSandboxMetadataValidate:
    """Tests for SandboxMetadataModel.validate()."""

    def test_valid_sandbox(self) -> None:
        from nexus.storage.models.infrastructure import SandboxMetadataModel

        s = SandboxMetadataModel(
            sandbox_id="sb_1",
            name="test",
            user_id="u1",
            zone_id="z1",
            provider="e2b",
            status="active",
        )
        s.validate()

    def test_invalid_provider(self) -> None:
        from nexus.storage.models.infrastructure import SandboxMetadataModel

        s = SandboxMetadataModel(
            sandbox_id="sb_1",
            name="test",
            user_id="u1",
            zone_id="z1",
            provider="invalid",
            status="active",
        )
        with pytest.raises(Exception, match="provider must be one of"):
            s.validate()

    def test_invalid_status(self) -> None:
        from nexus.storage.models.infrastructure import SandboxMetadataModel

        s = SandboxMetadataModel(
            sandbox_id="sb_1",
            name="test",
            user_id="u1",
            zone_id="z1",
            provider="e2b",
            status="invalid",
        )
        with pytest.raises(Exception, match="status must be one of"):
            s.validate()

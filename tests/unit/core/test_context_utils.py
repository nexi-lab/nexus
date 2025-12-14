"""Unit tests for context_utils module.

Tests cover utility functions for extracting and resolving context information:
- get_tenant_id: Extract tenant_id from context with defaults
- get_user_identity: Extract user identity (type, id) from context
- get_database_url: Resolve database URLs with environment variable priority
- resolve_skill_base_path: Determine skill base path based on context
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from nexus.core.context_utils import (
    get_database_url,
    get_tenant_id,
    get_user_identity,
    resolve_skill_base_path,
)


class TestGetTenantId:
    """Tests for get_tenant_id function."""

    def test_get_tenant_id_with_tenant_id(self):
        """Test extracting tenant_id from context with tenant_id attribute."""
        context = Mock()
        context.tenant_id = "acme_corp"

        result = get_tenant_id(context)
        assert result == "acme_corp"

    def test_get_tenant_id_with_none_tenant_id(self):
        """Test that None tenant_id defaults to 'default'."""
        context = Mock()
        context.tenant_id = None

        result = get_tenant_id(context)
        assert result == "default"

    def test_get_tenant_id_without_tenant_id_attribute(self):
        """Test that missing tenant_id attribute defaults to 'default'."""

        # Use a simple object without tenant_id attribute
        class SimpleContext:
            pass

        context = SimpleContext()
        result = get_tenant_id(context)
        assert result == "default"

    def test_get_tenant_id_with_none_context(self):
        """Test that None context defaults to 'default'."""
        result = get_tenant_id(None)
        assert result == "default"

    def test_get_tenant_id_with_empty_string(self):
        """Test that empty string tenant_id defaults to 'default'."""
        context = Mock()
        context.tenant_id = ""

        result = get_tenant_id(context)
        assert result == "default"


class TestGetUserIdentity:
    """Tests for get_user_identity function."""

    def test_get_user_identity_with_subject_type_and_id(self):
        """Test extracting identity from context with subject_type and subject_id."""
        context = Mock()
        context.subject_type = "user"
        context.subject_id = "alice"

        subject_type, subject_id = get_user_identity(context)
        assert subject_type == "user"
        assert subject_id == "alice"

    def test_get_user_identity_with_agent(self):
        """Test extracting agent identity from context."""
        context = Mock()
        context.subject_type = "agent"
        context.subject_id = "agent_001"

        subject_type, subject_id = get_user_identity(context)
        assert subject_type == "agent"
        assert subject_id == "agent_001"

    def test_get_user_identity_fallback_to_user_id(self):
        """Test that user_id is used when subject_id is not available."""
        context = Mock()
        context.subject_type = "user"
        context.subject_id = None
        context.user_id = "bob"

        subject_type, subject_id = get_user_identity(context)
        assert subject_type == "user"
        assert subject_id == "bob"

    def test_get_user_identity_fallback_to_user_legacy(self):
        """Test that legacy 'user' field is used when subject_id and user_id are not available."""
        context = Mock()
        context.subject_type = "user"
        context.subject_id = None
        context.user_id = None
        context.user = "charlie"

        subject_type, subject_id = get_user_identity(context)
        assert subject_type == "user"
        assert subject_id == "charlie"

    def test_get_user_identity_defaults_with_none_context(self):
        """Test that None context returns default values."""
        subject_type, subject_id = get_user_identity(None)
        assert subject_type == "user"
        assert subject_id is None

    def test_get_user_identity_defaults_subject_type(self):
        """Test that missing subject_type defaults to 'user'."""

        # Use a simple object with only subject_id
        class SimpleContext:
            def __init__(self):
                self.subject_id = "dave"

        context = SimpleContext()
        subject_type, subject_id = get_user_identity(context)
        assert subject_type == "user"
        assert subject_id == "dave"

    def test_get_user_identity_none_subject_type_defaults(self):
        """Test that None subject_type defaults to 'user'."""
        context = Mock()
        context.subject_type = None
        context.subject_id = "eve"

        subject_type, subject_id = get_user_identity(context)
        assert subject_type == "user"
        assert subject_id == "eve"

    def test_get_user_identity_all_none_returns_none_id(self):
        """Test that when all identity fields are None, subject_id is None."""
        context = Mock()
        context.subject_type = "user"
        context.subject_id = None
        context.user_id = None
        context.user = None

        subject_type, subject_id = get_user_identity(context)
        assert subject_type == "user"
        assert subject_id is None


class TestGetDatabaseUrl:
    """Tests for get_database_url function."""

    def test_get_database_url_from_env_var(self):
        """Test that TOKEN_MANAGER_DB environment variable takes priority."""
        obj = Mock()
        with pytest.MonkeyPatch().context() as m:
            m.setenv("TOKEN_MANAGER_DB", "postgresql://localhost/test")
            result = get_database_url(obj)
            assert result == "postgresql://localhost/test"

    def test_get_database_url_from_config_db_path(self):
        """Test that obj._config.db_path is used when env var is not set."""
        obj = Mock()
        obj._config = Mock()
        obj._config.db_path = "sqlite:///config.db"

        with pytest.MonkeyPatch().context() as m:
            m.delenv("TOKEN_MANAGER_DB", raising=False)
            result = get_database_url(obj)
            assert result == "sqlite:///config.db"

    def test_get_database_url_from_obj_db_path(self):
        """Test that obj.db_path is used when config is not available."""

        # Use a simple object without _config
        class SimpleObj:
            def __init__(self):
                self.db_path = "sqlite:///obj.db"

        obj = SimpleObj()

        with pytest.MonkeyPatch().context() as m:
            m.delenv("TOKEN_MANAGER_DB", raising=False)
            result = get_database_url(obj)
            assert result == "sqlite:///obj.db"

    def test_get_database_url_from_metadata(self):
        """Test that obj.metadata.database_url is used as fallback."""

        # Use a simple object without _config or db_path
        class MetadataObj:
            def __init__(self):
                self.metadata = Mock()
                self.metadata.database_url = "postgresql://localhost/metadata"

        obj = MetadataObj()

        with pytest.MonkeyPatch().context() as m:
            m.delenv("TOKEN_MANAGER_DB", raising=False)
            result = get_database_url(obj)
            assert result == "postgresql://localhost/metadata"

    def test_get_database_url_priority_order(self):
        """Test that priority order is correct: env > config > obj.db_path > metadata."""
        obj = Mock()
        obj._config = Mock()
        obj._config.db_path = "sqlite:///config.db"
        obj.db_path = "sqlite:///obj.db"
        obj.metadata = Mock()
        obj.metadata.database_url = "postgresql://localhost/metadata"

        # Env var should take priority
        with pytest.MonkeyPatch().context() as m:
            m.setenv("TOKEN_MANAGER_DB", "postgresql://localhost/env")
            result = get_database_url(obj)
            assert result == "postgresql://localhost/env"

        # Config should take priority over obj.db_path and metadata
        with pytest.MonkeyPatch().context() as m:
            m.delenv("TOKEN_MANAGER_DB", raising=False)
            result = get_database_url(obj)
            assert result == "sqlite:///config.db"

    def test_get_database_url_raises_when_none_available(self):
        """Test that RuntimeError is raised when no database URL is configured."""

        # Use a simple object with no database configuration
        class EmptyObj:
            pass

        obj = EmptyObj()

        with pytest.MonkeyPatch().context() as m:
            m.delenv("TOKEN_MANAGER_DB", raising=False)
            with pytest.raises(RuntimeError, match="No database path configured"):
                get_database_url(obj)

    def test_get_database_url_with_none_config(self):
        """Test that None _config is handled correctly."""
        obj = Mock()
        obj._config = None
        obj.db_path = "sqlite:///obj.db"

        with pytest.MonkeyPatch().context() as m:
            m.delenv("TOKEN_MANAGER_DB", raising=False)
            result = get_database_url(obj)
            assert result == "sqlite:///obj.db"

    def test_get_database_url_with_empty_config_db_path(self):
        """Test that empty config.db_path is skipped."""
        obj = Mock()
        obj._config = Mock()
        obj._config.db_path = ""
        obj.db_path = "sqlite:///obj.db"

        with pytest.MonkeyPatch().context() as m:
            m.delenv("TOKEN_MANAGER_DB", raising=False)
            result = get_database_url(obj)
            assert result == "sqlite:///obj.db"

    def test_get_database_url_with_none_db_path(self):
        """Test that None db_path is skipped."""
        obj = Mock()
        obj._config = Mock()
        obj._config.db_path = None
        obj.db_path = None
        obj.metadata = Mock()
        obj.metadata.database_url = "postgresql://localhost/metadata"

        with pytest.MonkeyPatch().context() as m:
            m.delenv("TOKEN_MANAGER_DB", raising=False)
            result = get_database_url(obj)
            assert result == "postgresql://localhost/metadata"


class TestResolveSkillBasePath:
    """Tests for resolve_skill_base_path function."""

    def test_resolve_skill_base_path_with_user_id(self):
        """Test that user_id takes priority for skill base path."""
        context = Mock()
        context.user_id = "alice"
        context.tenant_id = "acme"

        result = resolve_skill_base_path(context)
        assert result == "/skills/users/alice/"

    def test_resolve_skill_base_path_with_tenant_id_only(self):
        """Test that tenant_id is used when user_id is not available."""
        context = Mock()
        context.user_id = None
        context.tenant_id = "acme"

        result = resolve_skill_base_path(context)
        assert result == "/skills/tenants/acme/"

    def test_resolve_skill_base_path_defaults_to_system(self):
        """Test that system path is returned when no user_id or tenant_id."""
        context = Mock()
        context.user_id = None
        context.tenant_id = None

        result = resolve_skill_base_path(context)
        assert result == "/skills/system/"

    def test_resolve_skill_base_path_with_none_context(self):
        """Test that None context defaults to system path."""
        result = resolve_skill_base_path(None)
        assert result == "/skills/system/"

    def test_resolve_skill_base_path_without_user_id_attribute(self):
        """Test that missing user_id attribute falls back to tenant_id."""

        # Use a simple object with only tenant_id
        class SimpleContext:
            def __init__(self):
                self.tenant_id = "acme"

        context = SimpleContext()
        result = resolve_skill_base_path(context)
        assert result == "/skills/tenants/acme/"

    def test_resolve_skill_base_path_user_id_overrides_tenant_id(self):
        """Test that user_id takes priority even when tenant_id is present."""
        context = Mock()
        context.user_id = "bob"
        context.tenant_id = "acme"

        result = resolve_skill_base_path(context)
        assert result == "/skills/users/bob/"
        assert result != "/skills/tenants/acme/"

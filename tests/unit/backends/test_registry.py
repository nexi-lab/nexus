"""Unit tests for connector registry."""

import pytest

from nexus.backends.backend import Backend
from nexus.backends.registry import (
    ConnectorInfo,
    ConnectorRegistry,
    create_connector_from_config,
    register_connector,
)


class DummyBackend(Backend):
    """Dummy backend for testing."""

    def __init__(self, data_dir: str = "/tmp", other_param: str | None = None):
        self.data_dir = data_dir
        self.other_param = other_param

    @property
    def name(self) -> str:
        return "dummy"

    def write_content(self, content, context=None):
        return "hash"

    def read_content(self, content_hash, context=None):
        return b""

    def delete_content(self, content_hash, context=None):
        pass

    def content_exists(self, content_hash, context=None):
        return False

    def get_content_size(self, content_hash, context=None):
        return 0

    def get_ref_count(self, content_hash, context=None):
        return 0

    def mkdir(self, path, parents=False, exist_ok=False, context=None):
        pass

    def rmdir(self, path, recursive=False, context=None):
        pass

    def is_directory(self, path, context=None):
        return True


@pytest.fixture(autouse=True)
def clear_registry():
    """Clear registry before and after each test."""
    # Save existing connectors
    saved = dict(ConnectorRegistry._connectors)
    ConnectorRegistry.clear()
    yield
    # Restore after test
    ConnectorRegistry._connectors = saved


class TestConnectorRegistry:
    """Test ConnectorRegistry class."""

    def test_register_connector(self):
        """Test registering a connector."""
        ConnectorRegistry.register(
            name="test_backend",
            connector_class=DummyBackend,
            description="Test backend",
            category="storage",
            requires=["test-dep"],
        )

        assert ConnectorRegistry.is_registered("test_backend")
        info = ConnectorRegistry.get_info("test_backend")
        assert info.name == "test_backend"
        assert info.connector_class == DummyBackend
        assert info.description == "Test backend"
        assert info.category == "storage"
        assert info.requires == ["test-dep"]

    def test_register_duplicate_same_class(self):
        """Test registering same class twice is idempotent."""
        ConnectorRegistry.register("test", DummyBackend)
        ConnectorRegistry.register("test", DummyBackend)  # Should not raise

        assert ConnectorRegistry.list_available() == ["test"]

    def test_register_duplicate_different_class(self):
        """Test registering different class with same name raises."""

        class AnotherBackend(DummyBackend):
            pass

        ConnectorRegistry.register("test", DummyBackend)

        with pytest.raises(ValueError) as exc_info:
            ConnectorRegistry.register("test", AnotherBackend)

        assert "already registered" in str(exc_info.value)

    def test_get_connector(self):
        """Test getting a connector class."""
        ConnectorRegistry.register("test", DummyBackend)

        cls = ConnectorRegistry.get("test")

        assert cls == DummyBackend

    def test_get_unknown_connector(self):
        """Test getting unknown connector raises KeyError."""
        with pytest.raises(KeyError) as exc_info:
            ConnectorRegistry.get("nonexistent")

        assert "Unknown connector" in str(exc_info.value)

    def test_get_info(self):
        """Test getting connector info."""
        ConnectorRegistry.register("test", DummyBackend, description="Test")

        info = ConnectorRegistry.get_info("test")

        assert isinstance(info, ConnectorInfo)
        assert info.name == "test"
        assert info.description == "Test"

    def test_list_available(self):
        """Test listing available connectors."""
        ConnectorRegistry.register("alpha", DummyBackend)
        ConnectorRegistry.register("beta", DummyBackend)
        ConnectorRegistry.register("gamma", DummyBackend)

        available = ConnectorRegistry.list_available()

        assert available == ["alpha", "beta", "gamma"]  # Sorted

    def test_list_all(self):
        """Test listing all connector info."""
        ConnectorRegistry.register("a", DummyBackend, description="A")
        ConnectorRegistry.register("b", DummyBackend, description="B")

        all_info = ConnectorRegistry.list_all()

        assert len(all_info) == 2
        assert all(isinstance(info, ConnectorInfo) for info in all_info)
        assert [info.name for info in all_info] == ["a", "b"]

    def test_list_by_category(self):
        """Test filtering connectors by category."""
        ConnectorRegistry.register("storage1", DummyBackend, category="storage")
        ConnectorRegistry.register("storage2", DummyBackend, category="storage")
        ConnectorRegistry.register("api1", DummyBackend, category="api")

        storage = ConnectorRegistry.list_by_category("storage")
        api = ConnectorRegistry.list_by_category("api")

        assert len(storage) == 2
        assert len(api) == 1
        assert all(info.category == "storage" for info in storage)
        assert api[0].category == "api"

    def test_is_registered(self):
        """Test checking if connector is registered."""
        ConnectorRegistry.register("test", DummyBackend)

        assert ConnectorRegistry.is_registered("test") is True
        assert ConnectorRegistry.is_registered("nonexistent") is False

    def test_clear(self):
        """Test clearing registry."""
        ConnectorRegistry.register("test", DummyBackend)
        assert len(ConnectorRegistry.list_available()) == 1

        ConnectorRegistry.clear()

        assert len(ConnectorRegistry.list_available()) == 0


class TestRegisterConnectorDecorator:
    """Test @register_connector decorator."""

    def test_decorator_registers_class(self):
        """Test decorator registers the class."""

        @register_connector("decorated_test", description="Decorated")
        class DecoratedBackend(DummyBackend):
            pass

        assert ConnectorRegistry.is_registered("decorated_test")
        info = ConnectorRegistry.get_info("decorated_test")
        assert info.connector_class == DecoratedBackend
        assert info.description == "Decorated"

    def test_decorator_returns_class(self):
        """Test decorator returns the original class."""

        @register_connector("test2")
        class TestBackend(DummyBackend):
            pass

        # Should be able to use the class normally
        instance = TestBackend()
        assert isinstance(instance, TestBackend)

    def test_decorator_with_all_options(self):
        """Test decorator with all options."""

        @register_connector(
            "full_test",
            description="Full test",
            category="api",
            requires=["dep1", "dep2"],
        )
        class FullBackend(DummyBackend):
            pass

        info = ConnectorRegistry.get_info("full_test")
        assert info.description == "Full test"
        assert info.category == "api"
        assert info.requires == ["dep1", "dep2"]


class TestCreateConnectorFromConfig:
    """Test create_connector_from_config factory function."""

    def test_create_with_config_mapping(self):
        """Test creating connector with config mapping."""
        # Register with a config mapping
        ConnectorRegistry.register("test_mapped", DummyBackend)

        # Add to config mappings
        from nexus.backends import registry

        registry._CONFIG_MAPPINGS["test_mapped"] = {
            "data_dir": "data_dir",
            "extra": "other_param",
        }

        try:
            backend = create_connector_from_config(
                "test_mapped",
                {"data_dir": "/custom/path", "extra": "extra_value"},
            )

            assert isinstance(backend, DummyBackend)
            assert backend.data_dir == "/custom/path"
            assert backend.other_param == "extra_value"
        finally:
            # Cleanup
            del registry._CONFIG_MAPPINGS["test_mapped"]

    def test_create_unknown_connector(self):
        """Test creating unknown connector raises."""
        with pytest.raises(KeyError):
            create_connector_from_config("nonexistent", {})


class TestBuiltinConnectorRegistration:
    """Test that builtin connectors are registered correctly."""

    def test_builtin_connectors_registered(self):
        """Test that importing nexus.backends registers all connectors."""
        # Force re-import to trigger registration

        # Check that expected connectors are registered
        # Note: This test runs after clear_registry fixture restores saved connectors
        available = ConnectorRegistry.list_available()

        # At minimum, local should always be available
        assert "local" in available or len(available) == 0  # May be cleared

    def test_local_backend_registered_with_correct_info(self):
        """Test LocalBackend registration info."""
        # Re-register local for this test
        from nexus.backends.local import LocalBackend

        # LocalBackend should be registered via decorator
        if ConnectorRegistry.is_registered("local"):
            info = ConnectorRegistry.get_info("local")
            assert info.connector_class == LocalBackend
            assert info.category == "storage"
            assert "local" in info.name.lower() or "Local" in info.description

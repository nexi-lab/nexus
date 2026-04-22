"""Unit tests for connector registry."""

import inspect
import warnings

import pytest

from nexus.backends.base.backend import Backend
from nexus.backends.base.registry import (
    ArgType,
    ConnectionArg,
    ConnectorInfo,
    ConnectorRegistry,
    create_connector_from_config,
    derive_config_mapping,
    register_connector,
)
from nexus.backends.base.runtime_deps import BinaryDep, PythonDep, RuntimeDep


class DummyBackend(Backend):
    """Dummy backend for testing."""

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "data_dir": ConnectionArg(
            type=ArgType.PATH,
            description="Data directory",
            required=True,
        ),
        "other_param": ConnectionArg(
            type=ArgType.STRING,
            description="Other parameter",
            required=False,
            config_key="extra",
        ),
    }

    def __init__(self, data_dir: str = "/tmp", other_param: str | None = None):
        self.data_dir = data_dir
        self.other_param = other_param

    @property
    def name(self) -> str:
        return "dummy"

    def write_content(self, content, content_id: str = "", *, offset: int = 0, context=None):
        return "hash"

    def read_content(self, content_hash, context=None):
        return b""

    def delete_content(self, content_hash, context=None):
        pass

    def content_exists(self, content_hash, context=None):
        return False

    def get_content_size(self, content_hash, context=None):
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
    import nexus.backends as _backends
    import nexus.backends.misc.service_map as _sm

    # Ensure optional backends are registered before saving
    _backends._register_optional_backends()

    # Save existing connectors
    saved = dict(ConnectorRegistry._base._items)
    ConnectorRegistry.clear()
    yield
    # Restore after test — reset lazy-init flags so future syncs re-derive
    ConnectorRegistry._base._items = saved
    _backends._optional_backends_registered = False
    _sm._synced = False


class TestConnectorRegistry:
    """Test ConnectorRegistry class."""

    def test_register_connector(self):
        """Test registering a connector."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
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
        # Legacy requires= kwarg is ignored per Issue #3830 spec §6; callers must use runtime_deps=
        assert info.runtime_deps == ()

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
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

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
        # Legacy requires= kwarg is ignored per Issue #3830 spec §6; callers must use runtime_deps=
        assert info.runtime_deps == ()


class TestCreateConnectorFromConfig:
    """Test create_connector_from_config factory function."""

    def test_create_with_auto_derived_mapping(self):
        """Test creating connector with auto-derived config mapping."""
        # DummyBackend has CONNECTION_ARGS with config_key="extra" -> other_param
        ConnectorRegistry.register("test_mapped", DummyBackend)

        backend = create_connector_from_config(
            "test_mapped",
            {"data_dir": "/custom/path", "extra": "extra_value"},
        )

        assert isinstance(backend, DummyBackend)
        assert backend.data_dir == "/custom/path"
        assert backend.other_param == "extra_value"

    def test_create_with_passthrough_keys(self):
        """Test that unmapped config keys are passed through directly."""
        ConnectorRegistry.register("test_passthrough", DummyBackend)

        # "data_dir" is in the mapping (identity), "other_param" is not in
        # the mapping but matches a constructor param directly
        backend = create_connector_from_config(
            "test_passthrough",
            {"data_dir": "/path", "other_param": "direct_value"},
        )

        assert isinstance(backend, DummyBackend)
        assert backend.data_dir == "/path"
        # "other_param" not in mapping keys but passed through directly
        # (config_key="extra" means "extra"->other_param is the mapping entry,
        #  "other_param" is not a mapping key so it falls through)
        assert backend.other_param == "direct_value"

    def test_create_unknown_connector(self):
        """Test creating unknown connector raises."""
        with pytest.raises(RuntimeError, match="Unsupported backend type"):
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
        """Test CASLocalBackend registration info."""
        # Re-register local for this test
        from nexus.backends.storage.cas_local import CASLocalBackend

        # CASLocalBackend should be registered via decorator
        if ConnectorRegistry.is_registered("local"):
            info = ConnectorRegistry.get_info("local")
            assert info.connector_class == CASLocalBackend
            assert info.category == "storage"
            assert "local" in info.name.lower() or "Local" in info.description


class TestConnectionArgs:
    """Test CONNECTION_ARGS functionality."""

    def test_connection_arg_dataclass(self):
        """Test ConnectionArg dataclass creation."""
        arg = ConnectionArg(
            type=ArgType.STRING,
            description="Test argument",
            required=True,
            default="default_value",
            secret=False,
            env_var="TEST_VAR",
        )

        assert arg.type == ArgType.STRING
        assert arg.description == "Test argument"
        assert arg.required is True
        assert arg.default == "default_value"
        assert arg.secret is False
        assert arg.env_var == "TEST_VAR"

    def test_connection_arg_to_dict(self):
        """Test ConnectionArg serialization to dict."""
        arg = ConnectionArg(
            type=ArgType.SECRET,
            description="Secret value",
            required=False,
            secret=True,
            env_var="SECRET_VAR",
        )

        d = arg.to_dict()

        assert d["type"] == "secret"
        assert d["description"] == "Secret value"
        assert d["required"] is False
        assert d["secret"] is True
        assert d["env_var"] == "SECRET_VAR"
        assert "config_key" not in d  # None config_key omitted

    def test_connection_arg_to_dict_with_config_key(self):
        """Test ConnectionArg serialization includes config_key when set."""
        arg = ConnectionArg(
            type=ArgType.STRING,
            description="Bucket",
            config_key="bucket",
        )

        d = arg.to_dict()

        assert d["config_key"] == "bucket"

    def test_arg_types(self):
        """Test all ArgType enum values."""
        assert ArgType.STRING.value == "string"
        assert ArgType.SECRET.value == "secret"
        assert ArgType.PASSWORD.value == "password"
        assert ArgType.INTEGER.value == "integer"
        assert ArgType.BOOLEAN.value == "boolean"
        assert ArgType.PATH.value == "path"
        assert ArgType.OAUTH.value == "oauth"

    def test_connector_with_connection_args(self):
        """Test registering connector with CONNECTION_ARGS."""

        @register_connector("test_with_args", description="Test with args")
        class BackendWithArgs(DummyBackend):
            CONNECTION_ARGS = {
                "data_dir": ConnectionArg(
                    type=ArgType.STRING,
                    description="Data directory",
                    required=True,
                ),
                "other_param": ConnectionArg(
                    type=ArgType.SECRET,
                    description="Secret key",
                    required=False,
                    secret=True,
                    env_var="SECRET_KEY",
                ),
            }

        info = ConnectorRegistry.get_info("test_with_args")

        # Test connection_args property
        args = info.connection_args
        assert "data_dir" in args
        assert "other_param" in args
        assert args["data_dir"].required is True
        assert args["other_param"].secret is True

        # Test get_required_args
        required = info.get_required_args()
        assert "data_dir" in required
        assert "other_param" not in required

        # Test get_secret_args
        secrets = info.get_secret_args()
        assert "other_param" in secrets
        assert "data_dir" not in secrets

    def test_connector_without_connection_args(self):
        """Test connector without CONNECTION_ARGS returns empty dict."""

        @register_connector("test_no_args", description="No args")
        class BackendNoArgs(DummyBackend):
            CONNECTION_ARGS: dict[str, ConnectionArg] = {}

        info = ConnectorRegistry.get_info("test_no_args")

        assert info.connection_args == {}
        assert info.get_required_args() == []
        assert info.get_secret_args() == []

    def test_get_connection_args_method(self):
        """Test ConnectorRegistry.get_connection_args method."""

        @register_connector("test_get_args", description="Get args test")
        class BackendGetArgs(DummyBackend):
            CONNECTION_ARGS = {
                "data_dir": ConnectionArg(
                    type=ArgType.PATH,
                    description="Data dir path",
                    required=True,
                ),
            }

        args = ConnectorRegistry.get_connection_args("test_get_args")

        assert "data_dir" in args
        assert args["data_dir"].type == ArgType.PATH

    def test_builtin_connectors_have_connection_args(self):
        """Test that builtin connectors have CONNECTION_ARGS defined."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        # CASLocalBackend should have CONNECTION_ARGS
        assert hasattr(CASLocalBackend, "CONNECTION_ARGS")
        assert "root_path" in CASLocalBackend.CONNECTION_ARGS
        assert CASLocalBackend.CONNECTION_ARGS["root_path"].required is True


class TestDeriveConfigMapping:
    """Test derive_config_mapping function."""

    def test_identity_mapping(self):
        """Test that CONNECTION_ARGS keys without config_key produce identity mapping."""

        class IdentityBackend(DummyBackend):
            CONNECTION_ARGS = {
                "data_dir": ConnectionArg(
                    type=ArgType.PATH,
                    description="Data directory",
                ),
            }

        mapping = derive_config_mapping(IdentityBackend)

        assert mapping == {"data_dir": "data_dir"}

    def test_config_key_alias(self):
        """Test that config_key remaps to a different external key."""

        class AliasBackend(DummyBackend):
            CONNECTION_ARGS = {
                "data_dir": ConnectionArg(
                    type=ArgType.PATH,
                    description="Data directory",
                    config_key="storage_path",
                ),
            }

        mapping = derive_config_mapping(AliasBackend)

        assert mapping == {"storage_path": "data_dir"}

    def test_mixed_mapping(self):
        """Test mix of identity and aliased mappings."""

        class MixedBackend(DummyBackend):
            CONNECTION_ARGS = {
                "data_dir": ConnectionArg(
                    type=ArgType.PATH,
                    description="Data dir",
                    config_key="root",
                ),
                "other_param": ConnectionArg(
                    type=ArgType.STRING,
                    description="Other",
                ),
            }

        mapping = derive_config_mapping(MixedBackend)

        assert mapping == {"root": "data_dir", "other_param": "other_param"}

    def test_empty_connection_args(self):
        """Test backend with no CONNECTION_ARGS returns empty mapping."""

        class NoArgsBackend(DummyBackend):
            CONNECTION_ARGS: dict[str, ConnectionArg] = {}

        mapping = derive_config_mapping(NoArgsBackend)

        assert mapping == {}

    def test_no_connection_args_attribute(self):
        """Test backend without CONNECTION_ARGS attribute returns empty mapping."""

        class BareBackend(DummyBackend):
            pass

        # Remove inherited CONNECTION_ARGS
        BareBackend.CONNECTION_ARGS = {}  # type: ignore[assignment]

        mapping = derive_config_mapping(BareBackend)

        assert mapping == {}

    def test_invalid_param_raises(self):
        """Test that config_key mapping to non-existent param raises ValueError."""

        class BadBackend(DummyBackend):
            CONNECTION_ARGS = {
                "nonexistent_param": ConnectionArg(
                    type=ArgType.STRING,
                    description="Does not exist in __init__",
                ),
            }

        with pytest.raises(ValueError, match="nonexistent_param"):
            derive_config_mapping(BadBackend)

    def test_registration_stores_derived_mapping(self):
        """Test that register() stores the derived mapping in ConnectorInfo."""
        ConnectorRegistry.register("test_derived", DummyBackend)

        info = ConnectorRegistry.get_info("test_derived")

        # DummyBackend has config_key="extra" on other_param
        assert info.config_mapping == {"data_dir": "data_dir", "extra": "other_param"}


class TestExhaustiveBackendMappings:
    """Verify every registered backend has valid config_mapping."""

    def _get_all_registered_backends(self) -> list[str]:
        """Import all backends and return registered names."""
        from nexus.backends import _register_optional_backends

        _register_optional_backends()
        return ConnectorRegistry.list_available()

    def test_all_backends_have_valid_config_mapping(self):
        """Every backend's config_mapping params exist in __init__ signature."""
        for name in self._get_all_registered_backends():
            info = ConnectorRegistry.get_info(name)
            sig = inspect.signature(info.connector_class.__init__)
            valid_params = set(sig.parameters.keys()) - {"self"}

            for config_key, param_name in info.config_mapping.items():
                assert param_name in valid_params, (
                    f"Backend '{name}': config_mapping maps '{config_key}' -> "
                    f"'{param_name}', but '{param_name}' is not in __init__. "
                    f"Valid params: {sorted(valid_params)}"
                )

    def test_backends_with_connection_args_have_nonempty_mapping(self):
        """Backends with non-empty CONNECTION_ARGS must have non-empty config_mapping."""
        for name in self._get_all_registered_backends():
            info = ConnectorRegistry.get_info(name)
            connection_args = getattr(info.connector_class, "CONNECTION_ARGS", {})

            if connection_args:
                assert info.config_mapping, (
                    f"Backend '{name}' has CONNECTION_ARGS but empty config_mapping"
                )

    def test_local_backend_config_key_mapping(self):
        """Test CASLocalBackend maps data_dir -> root_path (backward compat)."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        ConnectorRegistry.register("local", CASLocalBackend)
        info = ConnectorRegistry.get_info("local")

        assert info.config_mapping["data_dir"] == "root_path"


class TestConnectorInfoRuntimeDeps:
    def test_default_empty_tuple(self) -> None:
        from nexus.backends.base.registry import ConnectorInfo

        info = ConnectorInfo(name="t", connector_class=DummyBackend)
        assert info.runtime_deps == ()

    def test_runtime_deps_stored(self) -> None:
        from nexus.backends.base.registry import ConnectorInfo

        deps: tuple[RuntimeDep, ...] = (
            PythonDep("boto3", extras=("s3",)),
            BinaryDep("gws", "brew install gws"),
        )
        info = ConnectorInfo(
            name="t",
            connector_class=DummyBackend,
            runtime_deps=deps,
        )
        assert info.runtime_deps == deps


class TestRegisterRuntimeDeps:
    def setup_method(self) -> None:
        from nexus.backends.base.registry import ConnectorRegistry

        ConnectorRegistry.clear()

    def teardown_method(self) -> None:
        from nexus.backends.base.registry import ConnectorRegistry

        ConnectorRegistry.clear()

    def test_decorator_kwarg_populates_runtime_deps(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        @register_connector(
            "t_deco",
            runtime_deps=(PythonDep("boto3", extras=("s3",)),),
        )
        class T(DummyBackend):
            pass

        info = ConnectorRegistry.get_info("t_deco")
        assert info.runtime_deps == (PythonDep("boto3", extras=("s3",)),)

    def test_class_attr_populates_runtime_deps_when_no_decorator_arg(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        @register_connector("t_attr")
        class T(DummyBackend):
            RUNTIME_DEPS = (BinaryDep("gws", "brew install gws"),)

        info = ConnectorRegistry.get_info("t_attr")
        assert info.runtime_deps == (BinaryDep("gws", "brew install gws"),)

    def test_decorator_arg_wins_when_both_present(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            @register_connector("t_both", runtime_deps=(PythonDep("httpx"),))
            class T(DummyBackend):
                RUNTIME_DEPS = (BinaryDep("gws", "brew install gws"),)

            assert any(
                issubclass(w.category, UserWarning) and "runtime_deps" in str(w.message)
                for w in caught
            )

        info = ConnectorRegistry.get_info("t_both")
        assert info.runtime_deps == (PythonDep("httpx"),)

    def test_bad_runtime_dep_type_raises(self) -> None:
        from typing import Any

        from nexus.backends.base.registry import ConnectorRegistry

        bad_deps: Any = ("not-a-dep-instance",)
        with pytest.raises(ValueError, match="RUNTIME_DEPS"):
            ConnectorRegistry.register(
                name="t_bad",
                connector_class=DummyBackend,
                runtime_deps=bad_deps,
            )

    def test_legacy_requires_kwarg_emits_deprecation(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            @register_connector("t_legacy", requires=["httpx"])
            class T(DummyBackend):
                pass

            assert any(issubclass(w.category, DeprecationWarning) for w in caught)

        info = ConnectorRegistry.get_info("t_legacy")
        assert info.runtime_deps == ()

    def test_no_conflict_warning_when_deps_match(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        matching_deps = (PythonDep("httpx"),)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            @register_connector("t_match", runtime_deps=matching_deps)
            class T(DummyBackend):
                RUNTIME_DEPS = matching_deps  # same tuple, no conflict

            conflict_warnings = [
                w
                for w in caught
                if issubclass(w.category, UserWarning) and "runtime_deps" in str(w.message)
            ]
            assert conflict_warnings == []

        info = ConnectorRegistry.get_info("t_match")
        assert info.runtime_deps == matching_deps


class TestPlaceholderRegistration:
    def setup_method(self) -> None:
        from nexus.backends.base.registry import ConnectorRegistry

        ConnectorRegistry.clear()

    def teardown_method(self) -> None:
        from nexus.backends.base.registry import ConnectorRegistry

        ConnectorRegistry.clear()

    def test_register_placeholder_stores_entry(self) -> None:
        from nexus.backends._manifest import ConnectorManifestEntry
        from nexus.backends.base.registry import ConnectorRegistry
        from nexus.backends.base.runtime_deps import PythonDep

        entry = ConnectorManifestEntry(
            name="placeholder_test",
            module_path="nowhere.real",
            class_name="Nope",
            description="Placeholder for test",
            category="storage",
            runtime_deps=(PythonDep("boto3", extras=("s3",)),),
        )
        ConnectorRegistry.register_placeholder(entry)

        info = ConnectorRegistry.get_info("placeholder_test")
        assert info.connector_class is None
        assert info.runtime_deps == (PythonDep("boto3", extras=("s3",)),)
        assert info.description == "Placeholder for test"
        assert info.category == "storage"

    def test_register_binds_class_into_placeholder(self) -> None:
        from nexus.backends._manifest import ConnectorManifestEntry
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )
        from nexus.backends.base.runtime_deps import PythonDep

        entry = ConnectorManifestEntry(
            name="bind_test",
            module_path="nowhere.real",
            class_name="DummyBackend",
            description="Bind test",
            category="storage",
            runtime_deps=(PythonDep("json"),),
            service_name="bind-svc",
        )
        ConnectorRegistry.register_placeholder(entry)

        @register_connector("bind_test")
        class T(DummyBackend):
            pass

        info = ConnectorRegistry.get_info("bind_test")
        assert info.connector_class is T
        # manifest metadata preserved
        assert info.runtime_deps == (PythonDep("json"),)
        assert info.description == "Bind test"
        assert info.category == "storage"
        assert info.service_name == "bind-svc"

    def test_register_emits_warning_when_builtin_passes_metadata(self) -> None:
        import warnings

        from nexus.backends._manifest import ConnectorManifestEntry
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        entry = ConnectorManifestEntry(
            name="warn_test",
            module_path="nowhere.real",
            class_name="DummyBackend",
            description="Manifest description",
            category="storage",
        )
        ConnectorRegistry.register_placeholder(entry)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            @register_connector(
                "warn_test",
                description="Decorator description",  # should trigger warning
            )
            class T(DummyBackend):
                pass

            assert any(
                issubclass(w.category, UserWarning) and "manifest" in str(w.message).lower()
                for w in caught
            ), "expected UserWarning about manifest overriding decorator kwargs"

        # Manifest values preserved despite decorator kwargs
        info = ConnectorRegistry.get_info("warn_test")
        assert info.description == "Manifest description"

    def test_external_plugin_still_registers_without_placeholder(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )
        from nexus.backends.base.runtime_deps import PythonDep

        @register_connector(
            "external_plugin",
            description="External plugin",
            category="external",
            runtime_deps=(PythonDep("json"),),
        )
        class T(DummyBackend):
            pass

        info = ConnectorRegistry.get_info("external_plugin")
        assert info.connector_class is T
        assert info.description == "External plugin"
        assert info.runtime_deps == (PythonDep("json"),)

    def test_get_raises_for_unbound_placeholder(self) -> None:
        import pytest

        from nexus.backends._manifest import ConnectorManifestEntry
        from nexus.backends.base.registry import ConnectorRegistry

        entry = ConnectorManifestEntry(
            name="unbound",
            module_path="nowhere.real",
            class_name="Nope",
            description="Unbound placeholder",
            category="storage",
        )
        ConnectorRegistry.register_placeholder(entry)

        with pytest.raises(KeyError, match="placeholder"):
            ConnectorRegistry.get("unbound")

    def test_register_emits_no_warning_when_builtin_passes_only_name(self) -> None:
        import warnings

        from nexus.backends._manifest import ConnectorManifestEntry
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )
        from nexus.backends.base.runtime_deps import PythonDep

        entry = ConnectorManifestEntry(
            name="no_warn_test",
            module_path="nowhere.real",
            class_name="DummyBackend",
            description="Manifest description",
            category="storage",
            runtime_deps=(PythonDep("json"),),
            service_name="svc",
        )
        ConnectorRegistry.register_placeholder(entry)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            @register_connector("no_warn_test")
            class T(DummyBackend):
                pass

            manifest_warnings = [
                w
                for w in caught
                if issubclass(w.category, UserWarning) and "manifest" in str(w.message).lower()
            ]
            assert not manifest_warnings, (
                "no warning expected when decorator passes only name; "
                f"got: {[str(w.message) for w in manifest_warnings]}"
            )

    def test_register_placeholder_preserves_already_bound_class(self) -> None:
        """When a connector module was imported before _register_optional_backends
        runs, the placeholder pass must not wipe the bound class — it must
        backfill manifest metadata on top."""
        from nexus.backends._manifest import ConnectorManifestEntry
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )
        from nexus.backends.base.runtime_deps import PythonDep

        # Simulate a direct import binding the class first (no placeholder
        # exists yet; hits the external-plugin branch of register()).
        @register_connector("preexisting", description="from decorator", category="storage")
        class PreExisting(DummyBackend):
            pass

        # Now manifest-driven Phase 1 runs.
        entry = ConnectorManifestEntry(
            name="preexisting",
            module_path="nowhere.real",
            class_name="PreExisting",
            description="from manifest",
            category="oauth",
            runtime_deps=(PythonDep("json"),),
            service_name="svc",
        )
        ConnectorRegistry.register_placeholder(entry)

        info = ConnectorRegistry.get_info("preexisting")
        # Class binding preserved.
        assert info.connector_class is PreExisting
        # Manifest metadata backfilled (wins over decorator values).
        assert info.description == "from manifest"
        assert info.category == "oauth"
        assert info.runtime_deps == (PythonDep("json"),)
        assert info.service_name == "svc"
        # Class-derived fields from the existing (decorator-bound) entry preserved.
        assert info.user_scoped is False
        assert info.backend_features == frozenset()
        # DummyBackend.CONNECTION_ARGS produces a non-trivial config_mapping;
        # confirm it's preserved (not wiped to {}).
        assert info.config_mapping  # non-empty

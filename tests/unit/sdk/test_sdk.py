"""Unit tests for nexus.sdk module.

Tests the SDK interface, ensuring it properly re-exports core functionality
and provides a clean, stable API for third-party tools.
"""

import pytest

from nexus.sdk import (
    Backend,
    BackendError,
    Config,
    FileNotFoundError,
    Filesystem,
    GCSBackend,
    InvalidPathError,
    LocalBackend,
    MetadataError,
    NamespaceConfig,
    NexusError,
    NexusFS,
    OperationContext,
    PermissionEnforcer,
    PermissionError,
    ReBACManager,
    ReBACTuple,
    RemoteNexusFS,
    Skill,
    SkillDependencyError,
    SkillExporter,
    SkillExportError,
    SkillManager,
    SkillManagerError,
    SkillMetadata,
    SkillNotFoundError,
    SkillParseError,
    SkillParser,
    SkillRegistry,
    ValidationError,
    connect,
    load_config,
)


class TestSDKImports:
    """Test that all SDK imports work correctly."""

    def test_connect_function_exists(self):
        """Test that connect function is available."""
        assert callable(connect)

    def test_config_imports(self):
        """Test that configuration classes are available."""
        assert Config is not None
        assert callable(load_config)

    def test_filesystem_imports(self):
        """Test that filesystem classes are available."""
        assert Filesystem is not None
        assert NexusFS is not None
        assert RemoteNexusFS is not None

    def test_backend_imports(self):
        """Test that backend classes are available."""
        assert Backend is not None
        assert LocalBackend is not None
        assert GCSBackend is not None

    def test_exception_imports(self):
        """Test that exception classes are available."""
        assert NexusError is not None
        assert FileNotFoundError is not None
        assert PermissionError is not None
        assert BackendError is not None
        assert InvalidPathError is not None
        assert MetadataError is not None
        assert ValidationError is not None

    def test_permission_imports(self):
        """Test that permission classes are available."""
        assert OperationContext is not None
        assert PermissionEnforcer is not None

    def test_rebac_imports(self):
        """Test that ReBAC classes are available."""
        assert ReBACManager is not None
        assert ReBACTuple is not None

    def test_router_imports(self):
        """Test that router classes are available."""
        assert NamespaceConfig is not None

    def test_skills_imports(self):
        """Test that skills classes are available."""
        assert SkillRegistry is not None
        assert SkillExporter is not None
        assert SkillManager is not None
        assert SkillParser is not None
        assert Skill is not None
        assert SkillMetadata is not None
        assert SkillNotFoundError is not None
        assert SkillDependencyError is not None
        assert SkillManagerError is not None
        assert SkillParseError is not None
        assert SkillExportError is not None


class TestSDKConnect:
    """Test the SDK connect function."""

    def test_connect_returns_filesystem(self, tmp_path):
        """Test that connect returns a Filesystem instance."""
        nx = connect(config={"data_dir": str(tmp_path)})
        assert isinstance(nx, Filesystem)
        assert isinstance(nx, NexusFS)

    def test_connect_with_dict_config(self, tmp_path):
        """Test connect with dictionary configuration."""
        nx = connect(
            config={
                "data_dir": str(tmp_path),
                "backend": "local",
            }
        )
        assert isinstance(nx, NexusFS)

    def test_connect_with_path_config(self, tmp_path):
        """Test connect with path to config file."""
        config_file = tmp_path / "nexus.yaml"
        config_file.write_text("backend: local\n")

        nx = connect(config=str(config_file))
        assert isinstance(nx, NexusFS)


class TestSDKOperations:
    """Test SDK operations work correctly."""

    def test_basic_file_operations(self, tmp_path):
        """Test basic file operations through SDK."""
        nx = connect(config={"data_dir": str(tmp_path), "enforce_permissions": False})

        # Write
        nx.write("/test.txt", b"Hello SDK")

        # Read
        content = nx.read("/test.txt")
        assert content == b"Hello SDK"

        # Exists
        assert nx.exists("/test.txt")

        # Delete
        nx.delete("/test.txt")
        assert not nx.exists("/test.txt")

    def test_list_operations(self, tmp_path):
        """Test list operations through SDK."""
        nx = connect(config={"data_dir": str(tmp_path), "enforce_permissions": False})

        nx.write("/file1.txt", b"content1")
        nx.write("/file2.txt", b"content2")

        files = nx.list("/")
        assert "/file1.txt" in files
        assert "/file2.txt" in files

    def test_glob_operations(self, tmp_path):
        """Test glob operations through SDK."""
        nx = connect(config={"data_dir": str(tmp_path), "enforce_permissions": False})

        nx.write("/test.txt", b"content")
        nx.write("/test.py", b"code")

        txt_files = list(nx.glob("*.txt"))
        assert any("/test.txt" in f for f in txt_files)

    def test_mkdir_operations(self, tmp_path):
        """Test mkdir operations through SDK."""
        nx = connect(config={"data_dir": str(tmp_path), "enforce_permissions": False})

        nx.mkdir("/testdir")
        assert nx.is_directory("/testdir")


class TestSDKBackwardCompatibility:
    """Test backward compatibility with existing code."""

    def test_sdk_compatible_with_core(self, tmp_path):
        """Test that SDK returns same types as core modules."""
        from nexus import connect as core_connect

        nx_sdk = connect(config={"data_dir": str(tmp_path / "sdk")})
        nx_core = core_connect(config={"data_dir": str(tmp_path / "core")})

        # Should return same type
        assert type(nx_sdk).__name__ == type(nx_core).__name__

    def test_sdk_provides_cleaner_imports(self):
        """Test that SDK provides cleaner import names."""
        from nexus.sdk import FileNotFoundError as SDKFileNotFound
        from nexus.sdk import PermissionError as SDKPermissionError

        # SDK provides cleaner import paths (even though underlying class name is prefixed)
        # The important part is developers can import with clean names
        assert SDKFileNotFound is not None
        assert SDKPermissionError is not None

        # Can use with cleaner name in code
        with pytest.raises(SDKFileNotFound):
            raise SDKFileNotFound("test")


class TestSDKDocumentation:
    """Test that SDK has proper documentation."""

    def test_sdk_module_has_docstring(self):
        """Test that SDK module has documentation."""
        import nexus.sdk

        assert nexus.sdk.__doc__ is not None
        assert "SDK" in nexus.sdk.__doc__

    def test_connect_has_docstring(self):
        """Test that connect function has documentation."""
        assert connect.__doc__ is not None
        assert "Connect to Nexus filesystem" in connect.__doc__

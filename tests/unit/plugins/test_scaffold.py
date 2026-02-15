"""Tests for plugin scaffold generator."""

import tempfile
from pathlib import Path

import pytest

from nexus.plugins.scaffold import PLUGIN_TYPES, scaffold_plugin


class TestScaffoldPlugin:
    """Test plugin scaffold generation."""

    def test_creates_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("test-plugin", Path(tmpdir))
            project_dir = Path(result["project_dir"])
            assert project_dir.exists()
            assert project_dir.name == "nexus-plugin-test-plugin"

    def test_creates_all_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("my-tool", Path(tmpdir))
            files = result["files_created"]

            # Should create at least: pyproject.toml, __init__.py, plugin.py, test, README
            assert len(files) >= 5

            project_dir = Path(result["project_dir"])
            assert (project_dir / "pyproject.toml").exists()
            assert (project_dir / "README.md").exists()
            assert (project_dir / "src" / "nexus_my_tool" / "__init__.py").exists()
            assert (project_dir / "src" / "nexus_my_tool" / "plugin.py").exists()
            assert (project_dir / "tests" / "test_plugin.py").exists()

    def test_pyproject_has_entry_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("demo", Path(tmpdir))
            pyproject = (Path(result["project_dir"]) / "pyproject.toml").read_text()

            assert '[project.entry-points."nexus.plugins"]' in pyproject
            assert "nexus_demo.plugin:DemoPlugin" in pyproject

    def test_plugin_class_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("my-backend", Path(tmpdir))
            assert result["class_name"] == "MyBackendPlugin"
            assert result["module_name"] == "nexus_my_backend"

    def test_plugin_file_contains_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("demo", Path(tmpdir))
            plugin_py = (
                Path(result["project_dir"]) / "src" / "nexus_demo" / "plugin.py"
            ).read_text()

            assert "class DemoPlugin(NexusPlugin):" in plugin_py
            assert "def metadata(self)" in plugin_py
            assert "def commands(self)" in plugin_py

    def test_test_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("demo", Path(tmpdir))
            test_py = (Path(result["project_dir"]) / "tests" / "test_plugin.py").read_text()

            assert "class TestPluginMetadata:" in test_py
            assert "DemoPlugin" in test_py


class TestScaffoldTypes:
    """Test different plugin type templates."""

    def test_generic_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("test", Path(tmpdir), plugin_type="generic")
            assert result["plugin_type"] == "generic"

    def test_storage_type_has_backend_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("test", Path(tmpdir), plugin_type="storage")
            plugin_py = (
                Path(result["project_dir"]) / "src" / "nexus_test" / "plugin.py"
            ).read_text()
            assert "Backend" in plugin_py

    def test_parser_type_has_parser_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("test", Path(tmpdir), plugin_type="parser")
            plugin_py = (
                Path(result["project_dir"]) / "src" / "nexus_test" / "plugin.py"
            ).read_text()
            assert "ParseProvider" in plugin_py


class TestScaffoldValidation:
    """Test input validation."""

    def test_rejects_empty_name(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            pytest.raises(ValueError, match="Invalid plugin name"),
        ):
            scaffold_plugin("", Path(tmpdir))

    def test_rejects_invalid_name(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            pytest.raises(ValueError, match="Invalid plugin name"),
        ):
            scaffold_plugin("my plugin!", Path(tmpdir))

    def test_rejects_unknown_type(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            pytest.raises(ValueError, match="Unknown plugin type"),
        ):
            scaffold_plugin("test", Path(tmpdir), plugin_type="unknown")

    def test_rejects_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create the target directory first
            (Path(tmpdir) / "nexus-plugin-test").mkdir()
            with pytest.raises(FileExistsError):
                scaffold_plugin("test", Path(tmpdir))

    def test_custom_author(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scaffold_plugin("test", Path(tmpdir), author="Jane Doe")
            pyproject = (Path(result["project_dir"]) / "pyproject.toml").read_text()
            assert "Jane Doe" in pyproject


class TestPluginTypes:
    """Test PLUGIN_TYPES constant."""

    def test_has_all_types(self) -> None:
        assert "generic" in PLUGIN_TYPES
        assert "storage" in PLUGIN_TYPES
        assert "parser" in PLUGIN_TYPES

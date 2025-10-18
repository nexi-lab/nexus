"""Unit tests for nexus.connect() function."""

import gc
import platform
import tempfile
import time
from pathlib import Path

import pytest

import nexus
from nexus import NexusFS


def cleanup_windows_db():
    """Force cleanup of database connections on Windows."""
    gc.collect()  # Force garbage collection to release connections
    if platform.system() == "Windows":
        time.sleep(0.05)  # 50ms delay for Windows file handle release


def test_connect_default_embedded_mode() -> None:
    """Test that connect() returns Embedded instance by default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = nexus.connect(config={"data_dir": tmpdir})

        assert isinstance(nx, NexusFS)
        # Resolve both paths to handle symlinks (e.g., /var vs /private/var on macOS)
        assert nx.backend.root_path.resolve() == Path(tmpdir).resolve()

        nx.close()
        cleanup_windows_db()


def test_connect_with_config_dict() -> None:
    """Test connect() with config dictionary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "mode": "embedded",
            "data_dir": tmpdir,
        }

        nx = nexus.connect(config=config)

        assert isinstance(nx, NexusFS)
        assert nx.backend.root_path.resolve() == Path(tmpdir).resolve()

        nx.close()
        cleanup_windows_db()


def test_connect_with_config_object() -> None:
    """Test connect() with NexusConfig object."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = nexus.NexusConfig(mode="embedded", data_dir=tmpdir)

        nx = nexus.connect(config=config)

        assert isinstance(nx, NexusFS)
        assert nx.backend.root_path.resolve() == Path(tmpdir).resolve()

        nx.close()
        cleanup_windows_db()


def test_connect_monolithic_mode_not_implemented() -> None:
    """Test that monolithic mode raises NotImplementedError."""
    config = {"mode": "monolithic", "url": "http://localhost:8000"}

    with pytest.raises(NotImplementedError) as exc_info:
        nexus.connect(config=config)

    assert "monolithic mode is not yet implemented" in str(exc_info.value)


def test_connect_distributed_mode_not_implemented() -> None:
    """Test that distributed mode raises NotImplementedError."""
    config = {"mode": "distributed", "url": "http://localhost:8000"}

    with pytest.raises(NotImplementedError) as exc_info:
        nexus.connect(config=config)

    assert "distributed mode is not yet implemented" in str(exc_info.value)


def test_connect_invalid_mode() -> None:
    """Test that invalid mode raises ValueError (caught by Pydantic validation)."""
    config = {"mode": "invalid"}

    # Pydantic validates before connect() is called
    with pytest.raises(ValueError):
        nexus.connect(config=config)


def test_connect_functional_workflow() -> None:
    """Test full workflow using connect()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Connect
        nx = nexus.connect(config={"data_dir": tmpdir})

        # Write
        nx.write("/test.txt", b"Hello, Nexus!")

        # Read
        content = nx.read("/test.txt")
        assert content == b"Hello, Nexus!"

        # List
        files = nx.list()
        assert "/test.txt" in files

        # Delete
        nx.delete("/test.txt")
        assert not nx.exists("/test.txt")

        nx.close()
        cleanup_windows_db()


def test_connect_context_manager() -> None:
    """Test using connect() result as context manager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with nexus.connect(config={"data_dir": tmpdir, "auto_parse": False}) as nx:
            nx.write("/test.txt", b"Content")
            assert nx.exists("/test.txt")
        cleanup_windows_db()


def test_connect_auto_discover() -> None:
    """Test that connect() can auto-discover config."""
    # Without any config, should use defaults (./nexus-data)
    # This test just verifies it doesn't crash
    nx = nexus.connect()
    assert isinstance(nx, NexusFS)
    nx.close()
    cleanup_windows_db()

    # Clean up default directory if created
    default_dir = Path("./nexus-data")
    if default_dir.exists():
        import shutil

        shutil.rmtree(default_dir)

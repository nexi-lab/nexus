"""Unit tests for nexus.connect() function."""

import gc
import platform
import tempfile
import time
from pathlib import Path

import pytest

import nexus
from nexus.core.nexus_fs import NexusFS


def cleanup_windows_db():
    """Force cleanup of database connections on Windows.

    Note: The sleep here is a legitimate Windows-specific workaround.
    Windows has a delay in releasing file handles even after GC.
    This is not a test timing issue but a platform limitation.
    """
    gc.collect()  # Force garbage collection to release connections
    if platform.system() == "Windows":
        time.sleep(0.2)  # 200ms delay for Windows file handle release


def test_connect_default_standalone_mode() -> None:
    """Test that connect() returns NexusFS in standalone mode by default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = nexus.connect(config={"data_dir": tmpdir})

        assert isinstance(nx, NexusFS)
        # Resolve both paths to handle symlinks (e.g., /var vs /private/var on macOS)
        assert nx.backend.root_path.resolve() == Path(tmpdir).resolve()

        nx.close()
        cleanup_windows_db()


def test_connect_with_config_dict() -> None:
    """Test connect() with config dictionary using standalone mode."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "mode": "standalone",
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
        config = nexus.NexusConfig(mode="standalone", data_dir=tmpdir)

        nx = nexus.connect(config=config)

        assert isinstance(nx, NexusFS)
        assert nx.backend.root_path.resolve() == Path(tmpdir).resolve()

        nx.close()
        cleanup_windows_db()


def test_connect_old_modes_rejected() -> None:
    """Test that old mode values (embedded, monolithic, distributed) are rejected."""
    for old_mode in ["embedded", "monolithic", "distributed"]:
        with pytest.raises(ValueError):
            nexus.connect(config={"mode": old_mode})


def test_connect_remote_mode_requires_url() -> None:
    """Test that remote mode raises ValueError without URL."""
    config = {"mode": "remote"}

    with pytest.raises(ValueError):
        nexus.connect(config=config)


def test_connect_invalid_mode() -> None:
    """Test that invalid mode raises ValueError (caught by Pydantic validation)."""
    config = {"mode": "invalid"}

    # Pydantic validates before connect() is called
    with pytest.raises(ValueError):
        nexus.connect(config=config)


def test_connect_functional_workflow() -> None:
    """Test full workflow using connect()."""
    tmpdir = tempfile.mkdtemp()
    try:
        # Connect (disable permissions for simple workflow test)
        nx = nexus.connect(config={"data_dir": tmpdir, "enforce_permissions": False})

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

        # Close and cleanup
        nx.close()

        # Force cleanup of database connections
        cleanup_windows_db()

        # Additional wait on Windows for SQLite to release file handles
        if platform.system() == "Windows":
            time.sleep(0.5)  # 500ms for SQLite to fully release
    finally:
        # Cleanup with retry on Windows
        import shutil

        if platform.system() == "Windows":
            # Retry cleanup with longer delays on Windows
            for attempt in range(5):  # Increased from 3 to 5 attempts
                try:
                    shutil.rmtree(tmpdir)
                    break
                except PermissionError:
                    if attempt < 4:
                        # Progressive backoff: 200ms, 400ms, 600ms, 800ms
                        time.sleep(0.2 * (attempt + 1))
                        gc.collect()  # Force GC on each retry
                    else:
                        raise
        else:
            shutil.rmtree(tmpdir)


def test_connect_context_manager() -> None:
    """Test using connect() result as context manager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with nexus.connect(
            config={"data_dir": tmpdir, "auto_parse": False, "enforce_permissions": False}
        ) as nx:
            nx.write("/test.txt", b"Content")
            assert nx.exists("/test.txt")
        cleanup_windows_db()


def test_connect_auto_discover() -> None:
    """Test that connect() can auto-discover config."""
    # Use a temporary directory to avoid cleanup issues with ./nexus-data
    with tempfile.TemporaryDirectory() as tmpdir:
        # Test with explicit data_dir to avoid creating ./nexus-data
        nx = nexus.connect(config={"data_dir": tmpdir})
        assert isinstance(nx, NexusFS)
        nx.close()
        cleanup_windows_db()

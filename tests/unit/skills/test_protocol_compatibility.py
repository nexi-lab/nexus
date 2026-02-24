"""Test that the narrow Skills Protocol is satisfied by core implementations.

The skills module defines a narrow NexusFilesystem Protocol with only the
7 methods it actually uses (read, write, list, exists, mkdir, delete, is_directory).
This test verifies that:

1. All narrow Protocol methods exist on the core ABC (subset check)
2. NexusFS concrete implementation satisfies the narrow Protocol
3. A minimal mock with just the 7 methods passes isinstance()
"""

import inspect

import pytest

from nexus.bricks.skills.protocols import NexusFilesystem as NexusFilesystemProtocol
from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC

try:
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    RaftMetadataStore.embedded("/tmp/_raft_probe")  # noqa: S108
    _raft_available = True
except Exception:
    _raft_available = False

# The 7 methods the skills module uses
REQUIRED_METHODS = {"read", "write", "list", "exists", "mkdir", "delete", "is_directory"}


def test_protocol_is_subset_of_abc() -> None:
    """Verify all narrow Protocol methods exist on the core ABC.

    This ensures the narrow Protocol is a valid subset of the full interface.
    """
    abc_methods = {
        name
        for name, _ in inspect.getmembers(NexusFilesystemABC, predicate=inspect.isfunction)
        if not name.startswith("_")
    }

    protocol_methods = set()
    for name in dir(NexusFilesystemProtocol):
        if name.startswith("_"):
            continue
        attr = getattr(NexusFilesystemProtocol, name, None)
        if attr is not None and callable(attr):
            protocol_methods.add(name)

    missing_from_abc = protocol_methods - abc_methods
    assert not missing_from_abc, (
        f"Narrow Protocol has methods not on ABC: {sorted(missing_from_abc)}"
    )


def test_protocol_covers_required_methods() -> None:
    """Verify the narrow Protocol defines all required methods."""
    protocol_methods = set()
    for name in dir(NexusFilesystemProtocol):
        if name.startswith("_"):
            continue
        attr = getattr(NexusFilesystemProtocol, name, None)
        if attr is not None and callable(attr):
            protocol_methods.add(name)

    missing = REQUIRED_METHODS - protocol_methods
    assert not missing, f"Protocol missing required methods: {sorted(missing)}"


@pytest.mark.skipif(not _raft_available, reason="Raft metastore not available")
def test_nexus_fs_satisfies_narrow_protocol() -> None:
    """Verify NexusFS implementation satisfies the narrow Protocol."""
    import tempfile
    from pathlib import Path

    from nexus import NexusFS
    from nexus.core.config import PermissionConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        metadata_store = RaftMetadataStore.embedded(str(Path(tmpdir) / "metadata"))
        nx = NexusFS(
            metadata_store=metadata_store,
            permissions=PermissionConfig(),
        )

        # Verify all required methods exist and are callable
        for method_name in REQUIRED_METHODS:
            assert callable(getattr(nx, method_name, None)), (
                f"NexusFS missing method: {method_name}"
            )

        # isinstance check with @runtime_checkable
        assert isinstance(nx, NexusFilesystemProtocol)


def test_minimal_mock_satisfies_protocol() -> None:
    """Verify a minimal mock with just the 7 methods passes isinstance()."""

    class MinimalFilesystem:
        def read(self, path, context=None, return_metadata=False):
            return b""

        def write(
            self, path, content, context=None, if_match=None, if_none_match=False, force=False
        ):
            return {}

        def list(
            self,
            path="/",
            recursive=True,
            details=False,
            show_parsed=True,
            context=None,
        ):
            return []

        def exists(self, path):
            return False

        def mkdir(self, path, parents=False, exist_ok=False):
            pass

        def delete(self, path):
            pass

        def is_directory(self, path, context=None):
            return False

    mock = MinimalFilesystem()
    assert isinstance(mock, NexusFilesystemProtocol)


def test_protocol_documentation() -> None:
    """Verify Protocol has proper documentation."""
    doc = NexusFilesystemProtocol.__doc__
    assert doc is not None, "Protocol should have docstring"
    assert "skills" in doc.lower(), "Documentation should mention skills module"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

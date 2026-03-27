"""Tests for PathLocalBackend — path-based local storage (no CAS)."""

from pathlib import Path

import pytest

from nexus.backends.storage.path_local import PathLocalBackend
from nexus.contracts.exceptions import BackendError
from nexus.contracts.types import OperationContext


def _ctx(backend_path: str = "/test.txt") -> OperationContext:
    """Helper to create a minimal OperationContext with backend_path."""
    return OperationContext(user_id="test", groups=[], backend_path=backend_path)


class TestPathLocalBasic:
    """Basic write/read/delete round-trip."""

    def test_write_read_roundtrip(self, tmp_path: Path) -> None:
        backend = PathLocalBackend(root_path=tmp_path)
        ctx = _ctx("/hello.txt")
        result = backend.write_content(b"hello world", context=ctx)
        assert result.size == 11
        assert result.content_id  # non-empty hash
        data = backend.read_content(result.content_id, context=ctx)
        assert data == b"hello world"

    def test_file_at_actual_path(self, tmp_path: Path) -> None:
        """Files should live at their actual path, not CAS-sharded."""
        backend = PathLocalBackend(root_path=tmp_path)
        backend.write_content(b"data", context=_ctx("/workspace/file.txt"))
        expected = tmp_path / "workspace" / "file.txt"
        assert expected.exists()
        assert expected.read_bytes() == b"data"

    def test_overwrite(self, tmp_path: Path) -> None:
        backend = PathLocalBackend(root_path=tmp_path)
        ctx = _ctx("/f.txt")
        backend.write_content(b"v1", context=ctx)
        backend.write_content(b"v2", context=ctx)
        assert backend.read_content("ignored", context=ctx) == b"v2"

    def test_delete(self, tmp_path: Path) -> None:
        backend = PathLocalBackend(root_path=tmp_path)
        ctx = _ctx("/del.txt")
        backend.write_content(b"gone", context=ctx)
        backend.delete_content("ignored", context=ctx)
        assert not (tmp_path / "del.txt").exists()

    def test_get_content_size(self, tmp_path: Path) -> None:
        backend = PathLocalBackend(root_path=tmp_path)
        ctx = _ctx("/sz.txt")
        backend.write_content(b"abcde", context=ctx)
        assert backend.get_content_size("ignored", context=ctx) == 5


class TestPathLocalRegistration:
    """Connector registry integration."""

    def test_registered_in_registry(self) -> None:
        from nexus.backends.base.registry import ConnectorRegistry

        cls = ConnectorRegistry.get("path_local")
        assert cls is PathLocalBackend

    def test_capabilities(self, tmp_path: Path) -> None:
        from nexus.contracts.backend_features import BackendFeature

        backend = PathLocalBackend(root_path=tmp_path)
        assert BackendFeature.ROOT_PATH in backend.capabilities
        assert backend.has_root_path is True
        assert backend.name == "path_local"


class TestPathLocalRequiresContext:
    """PathLocalBackend requires context.backend_path — verify error."""

    def test_write_without_context_raises(self, tmp_path: Path) -> None:
        backend = PathLocalBackend(root_path=tmp_path)
        with pytest.raises(BackendError, match="backend_path"):
            backend.write_content(b"data", context=None)

    def test_read_without_context_raises(self, tmp_path: Path) -> None:
        backend = PathLocalBackend(root_path=tmp_path)
        with pytest.raises(BackendError, match="backend_path"):
            backend.read_content("hash", context=None)

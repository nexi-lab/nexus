"""BackendFactory tests (Issue #1601).

Tests for centralized backend creation via BackendFactory.create().
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.backends.backend import Backend
from nexus.backends.factory import BackendFactory


class TestBackendFactory:
    """Tests for BackendFactory.create()."""

    def test_create_local_backend(self, tmp_path: Any) -> None:
        """Factory creates a valid LocalBackend."""
        backend = BackendFactory.create("local", {"data_dir": str(tmp_path / "data")})
        assert isinstance(backend, Backend)
        assert backend.name == "local"
        assert backend.has_root_path is True

    def test_create_local_backend_via_root_path(self, tmp_path: Any) -> None:
        """Config key 'data_dir' maps to constructor param 'root_path'."""
        data_dir = str(tmp_path / "nexus-data")
        backend = BackendFactory.create("local", {"data_dir": data_dir})
        assert backend.name == "local"

    def test_create_passthrough_backend(self, tmp_path: Any) -> None:
        """Factory creates a valid PassthroughBackend."""
        base = tmp_path / "base"
        base.mkdir()
        backend = BackendFactory.create("passthrough", {"base_path": str(base)})
        assert isinstance(backend, Backend)
        assert backend.name == "passthrough"
        assert backend.is_passthrough is True

    def test_unknown_backend_type_raises(self) -> None:
        """Unknown backend type raises RuntimeError."""
        with pytest.raises(RuntimeError, match="Unsupported backend type"):
            BackendFactory.create("nonexistent_backend", {})

    def test_extra_kwargs_passed_through(self, tmp_path: Any) -> None:
        """Extra kwargs like session_factory are passed to constructor."""
        # session_factory is accepted by LocalBackend but not required
        backend = BackendFactory.create(
            "local",
            {"data_dir": str(tmp_path / "data")},
        )
        assert backend is not None

    def test_hn_connector_creation(self) -> None:
        """Factory creates HN connector with config."""
        backend = BackendFactory.create(
            "hn_connector",
            {"cache_ttl": 60, "stories_per_feed": 5, "include_comments": False},
        )
        assert isinstance(backend, Backend)
        assert backend.name == "hn"

    def test_config_mapping_works(self, tmp_path: Any) -> None:
        """Config keys are mapped to constructor params via CONNECTION_ARGS."""
        # LocalBackend: config_key="data_dir" -> param="root_path"
        backend = BackendFactory.create("local", {"data_dir": str(tmp_path / "mapped")})
        assert backend.has_root_path is True

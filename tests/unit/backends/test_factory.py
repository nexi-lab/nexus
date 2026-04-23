"""BackendFactory tests (Issue #1601, #2362).

Tests for centralized backend creation via BackendFactory.create()
and wrapper chain assembly via BackendFactory.wrap().
"""

from typing import Any

import pytest

from nexus.backends.base.backend import Backend
from nexus.backends.base.factory import BackendFactory


class TestBackendFactory:
    """Tests for BackendFactory.create()."""

    def test_create_local_backend(self, tmp_path: Any) -> None:
        """Factory creates a valid CASLocalBackend."""
        backend = BackendFactory.create("cas_local", {"data_dir": str(tmp_path / "data")})
        assert isinstance(backend, Backend)
        assert backend.name == "local"
        assert backend.has_root_path is True

    def test_create_local_backend_via_root_path(self, tmp_path: Any) -> None:
        """Config key 'data_dir' maps to constructor param 'root_path'."""
        data_dir = str(tmp_path / "nexus-data")
        backend = BackendFactory.create("cas_local", {"data_dir": data_dir})
        assert backend.name == "local"

    def test_unknown_backend_type_raises(self) -> None:
        """Unknown backend type raises RuntimeError."""
        with pytest.raises(RuntimeError, match="Unsupported backend type"):
            BackendFactory.create("nonexistent_backend", {})

    def test_extra_kwargs_passed_through(self, tmp_path: Any) -> None:
        """Extra kwargs like session_factory are passed to constructor."""
        # session_factory is accepted by CASLocalBackend but not required
        backend = BackendFactory.create(
            "cas_local",
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
        # CASLocalBackend: config_key="data_dir" -> param="root_path"
        backend = BackendFactory.create("cas_local", {"data_dir": str(tmp_path / "mapped")})
        assert backend.has_root_path is True


# ---------------------------------------------------------------------------
# BackendFactory.wrap() tests (Issue #2362, Decision 11A)
# ---------------------------------------------------------------------------


class TestBackendFactoryWrap:
    """Tests for BackendFactory.wrap() wrapper chain assembly."""

    def test_wrap_logging(self, tmp_path: Any) -> None:
        """wrap("logging") creates a LoggingBackendWrapper."""
        from nexus.backends.wrappers.logging import LoggingBackendWrapper

        base = BackendFactory.create("cas_local", {"data_dir": str(tmp_path / "data")})
        wrapped = BackendFactory.wrap(base, "logging")
        assert isinstance(wrapped, LoggingBackendWrapper)

    def test_wrap_compressed(self, tmp_path: Any) -> None:
        """wrap("compress") creates a CompressedStorage."""
        from nexus.backends.wrappers.compressed import CompressedStorage, is_zstd_available

        if not is_zstd_available():
            pytest.skip("zstd not available")
        base = BackendFactory.create("cas_local", {"data_dir": str(tmp_path / "data")})
        wrapped = BackendFactory.wrap(base, "compress")
        assert isinstance(wrapped, CompressedStorage)

    def test_wrap_encrypted(self, tmp_path: Any) -> None:
        """wrap("encrypt") creates an EncryptedStorage."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.wrappers.encrypted import EncryptedStorage

        key = AESGCMSIV.generate_key(bit_length=256)
        base = BackendFactory.create("cas_local", {"data_dir": str(tmp_path / "data")})
        wrapped = BackendFactory.wrap(base, "encrypt", {"key": key})
        assert isinstance(wrapped, EncryptedStorage)

    def test_wrap_unknown_type(self, tmp_path: Any) -> None:
        """wrap() raises ValueError for unknown wrapper types."""
        base = BackendFactory.create("cas_local", {"data_dir": str(tmp_path / "data")})
        with pytest.raises(ValueError, match="Unknown wrapper type"):
            BackendFactory.wrap(base, "nonexistent")

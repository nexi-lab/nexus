from unittest.mock import MagicMock

import pytest

from nexus.backends.base.backend import Backend
from nexus.backends.base.cli_backend import PathCLIBackend
from nexus.backends.connectors.github.connector import GitHubConnector
from nexus.backends.storage.delegating import DelegatingBackend
from nexus.backends.storage.path_gcs import PathGCSBackend
from nexus.backends.storage.path_s3 import PathS3Backend


class _ConcreteBackend(Backend):
    @property
    def name(self) -> str:
        return "test"

    def write_content(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    def read_content(self, *args: object, **kwargs: object) -> bytes:
        raise NotImplementedError

    def delete_content(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    def get_content_size(self, *args: object, **kwargs: object) -> int:
        raise NotImplementedError

    def mkdir(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    def rmdir(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError


def test_path_s3_fingerprint_prefers_version_id_then_etag() -> None:
    backend = object.__new__(PathS3Backend)
    backend._s3_transport = MagicMock()
    backend._get_key_path = lambda path: path
    backend._s3_transport.fingerprint.return_value = "version:v123"

    assert backend.fingerprint("/file.txt") == "version:v123"
    backend._s3_transport.fingerprint.assert_called_once_with("file.txt")

    backend._s3_transport.fingerprint.reset_mock()
    backend._s3_transport.fingerprint.return_value = "etag:abc123"
    assert backend.fingerprint("/file.txt") == "etag:abc123"
    backend._s3_transport.fingerprint.assert_called_once_with("file.txt")


def test_backend_fingerprint_defaults_to_none() -> None:
    backend = _ConcreteBackend()

    assert backend.fingerprint("/file.txt") is None


def test_path_s3_fingerprint_uses_context_backend_path() -> None:
    context = MagicMock()
    context.backend_path = "context/file.txt"
    backend = object.__new__(PathS3Backend)
    backend._s3_transport = MagicMock()
    backend._get_key_path = MagicMock(side_effect=lambda path: f"prefix/{path}")
    backend._s3_transport.fingerprint.return_value = "version:v123"

    assert backend.fingerprint("/ignored.txt", context=context) == "version:v123"
    backend._get_key_path.assert_called_once_with("context/file.txt")
    backend._s3_transport.fingerprint.assert_called_once_with("prefix/context/file.txt")


def test_path_gcs_fingerprint_returns_generation() -> None:
    backend = object.__new__(PathGCSBackend)
    backend._gcs_transport = MagicMock()
    backend._get_key_path = lambda path: path
    backend._gcs_transport.reload_blob_metadata.return_value = {"generation": "456"}

    assert backend.fingerprint("/file.txt") == "456"


def test_path_gcs_fingerprint_uses_context_backend_path() -> None:
    context = MagicMock()
    context.backend_path = "context/file.txt"
    backend = object.__new__(PathGCSBackend)
    backend._gcs_transport = MagicMock()
    backend._get_key_path = MagicMock(side_effect=lambda path: f"prefix/{path}")
    backend._gcs_transport.reload_blob_metadata.return_value = {"generation": "456"}

    assert backend.fingerprint("/ignored.txt", context=context) == "456"
    backend._get_key_path.assert_called_once_with("context/file.txt")
    backend._gcs_transport.reload_blob_metadata.assert_called_once_with("prefix/context/file.txt")


def test_path_gcs_fingerprint_preserves_metadata_errors() -> None:
    backend = object.__new__(PathGCSBackend)
    backend._gcs_transport = MagicMock()
    backend._get_key_path = lambda path: path
    backend._gcs_transport.reload_blob_metadata.side_effect = RuntimeError("backend down")

    with pytest.raises(RuntimeError, match="backend down"):
        backend.fingerprint("/file.txt")


def test_cli_backend_fingerprint_defaults_to_none() -> None:
    backend = object.__new__(PathCLIBackend)
    assert backend.fingerprint("/issues/1_test.yaml") is None


def test_delegating_backend_fingerprint_delegates_to_inner() -> None:
    context = MagicMock()
    inner = MagicMock()
    inner.backend_features = frozenset()
    inner.fingerprint.return_value = "etag:inner"
    backend = DelegatingBackend(inner)

    assert backend.fingerprint("/file.txt", context=context) == "etag:inner"
    inner.fingerprint.assert_called_once_with("/file.txt", context=context)


def test_github_connector_fingerprint_uses_sha_blob_sha_then_etag() -> None:
    backend = object.__new__(GitHubConnector)
    backend.list_dir_metadata = MagicMock(
        return_value={
            "sha.yaml": {"sha": "sha123"},
            "blob.yaml": {"blob_sha": "blob456"},
            "etag.yaml": {"etag": "etag789"},
        }
    )

    assert backend.fingerprint("/issues/sha.yaml") == "sha123"
    assert backend.fingerprint("/issues/blob.yaml") == "blob456"
    assert backend.fingerprint("issues/etag.yaml") == "etag789"


def test_github_connector_fingerprint_falls_back_to_none_without_sha() -> None:
    backend = object.__new__(GitHubConnector)
    backend.list_dir_metadata = MagicMock(
        return_value={"1_test.yaml": {"number": 1, "title": "Test issue"}}
    )

    assert backend.fingerprint("/issues/1_test.yaml") is None

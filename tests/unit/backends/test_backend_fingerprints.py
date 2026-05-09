from unittest.mock import MagicMock

from nexus.backends.base.cli_backend import PathCLIBackend
from nexus.backends.connectors.github.connector import GitHubConnector
from nexus.backends.storage.path_gcs import PathGCSBackend
from nexus.backends.storage.path_s3 import PathS3Backend


def test_path_s3_fingerprint_prefers_version_id_then_etag() -> None:
    backend = object.__new__(PathS3Backend)
    backend._s3_transport = MagicMock()
    backend._get_key_path = lambda path: path
    backend._s3_transport.get_object_metadata.return_value = {
        "version_id": "v123",
        "etag": "abc123",
        "size": 1,
        "last_modified": None,
    }

    assert backend.fingerprint("/file.txt") == "v123"

    backend._s3_transport.get_object_metadata.return_value["version_id"] = "null"
    assert backend.fingerprint("/file.txt") == "etag:abc123"


def test_path_gcs_fingerprint_returns_generation() -> None:
    backend = object.__new__(PathGCSBackend)
    backend._gcs_transport = MagicMock()
    backend._get_key_path = lambda path: path
    backend._gcs_transport.get_generation.return_value = "456"

    assert backend.fingerprint("/file.txt") == "456"


def test_cli_backend_fingerprint_defaults_to_none() -> None:
    backend = object.__new__(PathCLIBackend)
    assert backend.fingerprint("/issues/1_test.yaml") is None


def test_github_connector_fingerprint_falls_back_to_none_without_sha() -> None:
    backend = object.__new__(GitHubConnector)
    backend.list_dir_metadata = MagicMock(
        return_value={"1_test.yaml": {"number": 1, "title": "Test issue"}}
    )

    assert backend.fingerprint("/issues/1_test.yaml") is None

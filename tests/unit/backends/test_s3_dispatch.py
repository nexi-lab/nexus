from __future__ import annotations

from collections.abc import Mapping

from nexus.backends.storage.path_s3 import PathS3Backend
from nexus.backends.transports.s3_transport import S3Transport
from nexus.contracts.types import OperationContext


class FakeS3Client:
    def __init__(self, response: Mapping[str, object]) -> None:
        self.response = dict(response)
        self.head_calls: list[dict[str, str]] = []
        self.get_calls: list[dict[str, str]] = []

    def head_object(self, **kwargs: str) -> dict[str, object]:
        self.head_calls.append(kwargs)
        return self.response

    def get_object(self, **kwargs: str) -> object:
        self.get_calls.append(kwargs)
        raise AssertionError("fingerprint must not download object content")


def _transport(response: Mapping[str, object]) -> tuple[S3Transport, FakeS3Client]:
    client = FakeS3Client(response)
    transport = S3Transport.__new__(S3Transport)
    transport.bucket_name = "test-bucket"
    transport.s3_client = client
    return transport, client


def test_s3_transport_fingerprint_prefers_version_without_downloading() -> None:
    transport, client = _transport(
        {
            "VersionId": "v1",
            "ETag": '"abc"',
            "ContentLength": 123,
        }
    )

    assert transport.fingerprint("docs/a.txt") == "version:v1"
    assert client.head_calls == [{"Bucket": "test-bucket", "Key": "docs/a.txt"}]
    assert client.get_calls == []


def test_s3_transport_fingerprint_falls_back_to_etag_then_size() -> None:
    transport_with_etag, _ = _transport(
        {
            "VersionId": "null",
            "ETag": '"abc"',
            "ContentLength": 123,
        }
    )
    transport_with_size, _ = _transport(
        {
            "VersionId": None,
            "ETag": "",
            "ContentLength": 456,
        }
    )

    assert transport_with_etag.fingerprint("docs/a.txt") == "etag:abc"
    assert transport_with_size.fingerprint("docs/a.txt") == "size:456"


class FakeFingerprintTransport:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def fingerprint(self, key: str) -> str:
        self.keys.append(key)
        return f"fp:{key}"


def _backend() -> tuple[PathS3Backend, FakeFingerprintTransport]:
    transport = FakeFingerprintTransport()
    backend = PathS3Backend.__new__(PathS3Backend)
    backend._s3_transport = transport
    backend.prefix = "prefix"
    return backend, transport


def test_s3_backend_fingerprint_maps_path_to_prefixed_key() -> None:
    backend, transport = _backend()

    assert backend.fingerprint("/docs/a.txt") == "fp:prefix/docs/a.txt"
    assert transport.keys == ["prefix/docs/a.txt"]


def test_s3_backend_fingerprint_prefers_context_backend_path() -> None:
    backend, transport = _backend()
    context = OperationContext(user_id="u", groups=[], backend_path="/actual/b.txt")

    assert backend.fingerprint("/ignored.txt", context=context) == "fp:prefix/actual/b.txt"
    assert transport.keys == ["prefix/actual/b.txt"]

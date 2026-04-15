"""Tests for S3 credential routing in _backend_factory.py.

S3 always uses boto3's native provider chain. The external sync framework
populates the profile store for `auth list` but does NOT inject credentials
into the S3 backend.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_spec() -> MagicMock:
    """Return a mock MountSpec for s3://my-bucket/prefix."""
    spec = MagicMock()
    spec.scheme = "s3"
    spec.authority = "my-bucket"
    spec.path = "/some-prefix"
    return spec


class TestS3AlwaysUsesNativeChain:
    """S3 backend always uses boto3 native chain, never injected credentials."""

    @patch("nexus.fs._external_sync_boot.ensure_external_sync")
    @patch(
        "nexus.fs._credentials.discover_credentials", return_value={"source": "credentials_file"}
    )
    @patch("nexus.backends.storage.path_s3.PathS3Backend")
    def test_s3_calls_ensure_sync_and_discover(
        self,
        mock_backend_cls: MagicMock,
        mock_discover: MagicMock,
        mock_sync: MagicMock,
    ) -> None:
        from nexus.fs._backend_factory import create_backend

        mock_backend_cls.return_value = MagicMock()
        result = create_backend(_make_spec())

        mock_sync.assert_called_once()
        mock_discover.assert_called_once_with("s3")
        mock_backend_cls.assert_called_once_with(
            bucket_name="my-bucket",
            prefix="some-prefix",
        )
        assert result is mock_backend_cls.return_value

    @patch("nexus.fs._credentials.discover_credentials", return_value={"source": "env"})
    @patch("nexus.backends.storage.path_s3.PathS3Backend")
    def test_s3_no_explicit_credentials_injected(
        self,
        mock_backend_cls: MagicMock,
        _mock_discover: MagicMock,
    ) -> None:
        """PathS3Backend is called with only bucket_name and prefix — no static keys."""
        from nexus.fs._backend_factory import create_backend

        mock_backend_cls.return_value = MagicMock()
        create_backend(_make_spec())

        call_kwargs = mock_backend_cls.call_args
        # Only bucket_name and prefix — no access_key_id, secret_access_key, etc.
        assert set(call_kwargs.kwargs.keys()) <= {"bucket_name", "prefix"} or (
            len(call_kwargs.args) <= 2 and not call_kwargs.kwargs.get("access_key_id")
        )

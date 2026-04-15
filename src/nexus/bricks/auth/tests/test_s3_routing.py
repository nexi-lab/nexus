"""Tests for S3 dual-path credential routing in _backend_factory.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.profile import AuthProfile, ProfileUsageStats


def _make_s3_profile() -> AuthProfile:
    """Return a mock S3 auth profile with external-cli backend."""
    return AuthProfile(
        id="s3/default",
        provider="s3",
        account_identifier="default",
        backend="external-cli",
        backend_key="aws-cli/default",
        usage_stats=ProfileUsageStats(),
    )


def _make_resolved_cred() -> ResolvedCredential:
    """Return a mock resolved credential with AWS keys."""
    return ResolvedCredential(
        kind="api_key",
        api_key="AKIAIOSFODNN7EXAMPLE",
        metadata={
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "session_token": "FwoGZXIvYXdzEA...",
            "region": "us-west-2",
        },
    )


def _make_spec() -> MagicMock:
    """Return a mock MountSpec for s3://my-bucket/prefix."""
    spec = MagicMock()
    spec.scheme = "s3"
    spec.authority = "my-bucket"
    spec.path = "/some-prefix"
    return spec


class TestS3RoutesProfileStore:
    """S3 routes through the profile store when a usable profile exists."""

    @patch("nexus.fs._backend_factory._resolve_external_credential")
    @patch("nexus.fs._backend_factory._try_profile_store_select")
    @patch("nexus.backends.storage.path_s3.PathS3Backend")
    def test_s3_routes_through_profile_store_when_populated(
        self,
        mock_backend_cls: MagicMock,
        mock_select: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        from nexus.fs._backend_factory import create_backend

        mock_select.return_value = _make_s3_profile()
        cred = _make_resolved_cred()
        mock_resolve.return_value = cred
        mock_backend_cls.return_value = MagicMock(name="PathS3BackendInstance")

        spec = _make_spec()
        result = create_backend(spec)

        mock_select.assert_called_once_with(provider="s3")
        mock_resolve.assert_called_once_with("aws-cli/default")
        mock_backend_cls.assert_called_once_with(
            bucket_name="my-bucket",
            prefix="some-prefix",
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="FwoGZXIvYXdzEA...",
            region_name="us-west-2",
        )
        assert result is mock_backend_cls.return_value


class TestS3FallsBackWhenNoProfile:
    """S3 falls back to discover_credentials() when no profile is available."""

    @patch("nexus.fs._backend_factory._try_profile_store_select", return_value=None)
    @patch("nexus.fs._credentials.discover_credentials", return_value={"source": "environment"})
    @patch("nexus.backends.storage.path_s3.PathS3Backend")
    def test_s3_falls_back_when_no_profile(
        self,
        mock_backend_cls: MagicMock,
        mock_discover: MagicMock,
        mock_select: MagicMock,
    ) -> None:
        from nexus.fs._backend_factory import create_backend

        mock_backend_cls.return_value = MagicMock(name="PathS3BackendInstance")

        spec = _make_spec()
        result = create_backend(spec)

        mock_select.assert_called_once_with(provider="s3")
        mock_discover.assert_called_once_with("s3")
        # Fallback path: PathS3Backend called without explicit credential kwargs
        mock_backend_cls.assert_called_once_with(
            bucket_name="my-bucket",
            prefix="some-prefix",
        )
        assert result is mock_backend_cls.return_value

    @patch("nexus.fs._backend_factory._resolve_external_credential", return_value=None)
    @patch("nexus.fs._backend_factory._try_profile_store_select")
    @patch("nexus.fs._credentials.discover_credentials", return_value={"source": "environment"})
    @patch("nexus.backends.storage.path_s3.PathS3Backend")
    def test_s3_falls_back_when_resolve_fails(
        self,
        mock_backend_cls: MagicMock,
        mock_discover: MagicMock,
        mock_select: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """Falls back when profile exists but credential resolution fails."""
        from nexus.fs._backend_factory import create_backend

        mock_select.return_value = _make_s3_profile()
        mock_backend_cls.return_value = MagicMock(name="PathS3BackendInstance")

        spec = _make_spec()
        result = create_backend(spec)

        mock_select.assert_called_once_with(provider="s3")
        mock_resolve.assert_called_once_with("aws-cli/default")
        mock_discover.assert_called_once_with("s3")
        mock_backend_cls.assert_called_once_with(
            bucket_name="my-bucket",
            prefix="some-prefix",
        )
        assert result is mock_backend_cls.return_value

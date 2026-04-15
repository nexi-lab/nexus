"""Tests for S3 dual-path credential routing in _backend_factory.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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


def _make_spec() -> MagicMock:
    """Return a mock MountSpec for s3://my-bucket/prefix."""
    spec = MagicMock()
    spec.scheme = "s3"
    spec.authority = "my-bucket"
    spec.path = "/some-prefix"
    return spec


class TestS3RoutesProfileStore:
    """S3 routes through the profile store when a usable profile exists."""

    @patch("nexus.fs._backend_factory._try_profile_store_select")
    @patch("nexus.backends.storage.path_s3.PathS3Backend")
    def test_s3_uses_boto3_native_chain_when_profile_found(
        self,
        mock_backend_cls: MagicMock,
        mock_select: MagicMock,
    ) -> None:
        """Profile found → PathS3Backend with no explicit credentials (boto3 resolves)."""
        from nexus.fs._backend_factory import create_backend

        mock_select.return_value = _make_s3_profile()
        mock_backend_cls.return_value = MagicMock(name="PathS3BackendInstance")

        spec = _make_spec()
        result = create_backend(spec)

        mock_select.assert_called_once()
        # No static credentials injected — boto3 native chain handles refresh
        mock_backend_cls.assert_called_once_with(
            bucket_name="my-bucket",
            prefix="some-prefix",
        )
        assert result is mock_backend_cls.return_value

    @patch.dict("os.environ", {"AWS_PROFILE": "work-prod"})
    @patch("nexus.fs._backend_factory._try_profile_store_select")
    @patch("nexus.backends.storage.path_s3.PathS3Backend")
    def test_s3_passes_aws_profile_to_selector(
        self,
        mock_backend_cls: MagicMock,
        mock_select: MagicMock,
    ) -> None:
        """AWS_PROFILE env var is forwarded to profile selection."""
        from nexus.fs._backend_factory import create_backend

        mock_select.return_value = None  # force fallback
        mock_backend_cls.return_value = MagicMock()

        with patch("nexus.fs._credentials.discover_credentials", return_value={"source": "env"}):
            create_backend(_make_spec())

        mock_select.assert_called_once_with(provider="s3", account="work-prod")


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

        result = create_backend(_make_spec())

        mock_select.assert_called_once()
        mock_discover.assert_called_once_with("s3")
        mock_backend_cls.assert_called_once_with(
            bucket_name="my-bucket",
            prefix="some-prefix",
        )
        assert result is mock_backend_cls.return_value

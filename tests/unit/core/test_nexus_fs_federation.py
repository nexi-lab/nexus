"""Unit tests for NexusFSFederationMixin."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestNexusFSFederationMixin:
    """Tests for federation mixin functionality."""

    def test_federation_disabled_by_default(self) -> None:
        """Test that federation is disabled by default."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation()

        assert mixin._federation_enabled is False
        assert mixin._federation_token is None
        assert mixin._local_box_id is None

    def test_federation_enabled_with_params(self) -> None:
        """Test enabling federation with parameters."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(
            federation_enabled=True,
            federation_token="test-token",
            local_box_id="box-a",
        )

        assert mixin._federation_enabled is True
        assert mixin._federation_token == "test-token"
        assert mixin._local_box_id == "box-a"

    def test_is_remote_path_when_disabled(self) -> None:
        """Test _is_remote_path returns False when federation disabled."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(federation_enabled=False)

        is_remote, info = mixin._is_remote_path("/some/path")

        assert is_remote is False
        assert info is None

    def test_is_remote_path_local_mount(self) -> None:
        """Test _is_remote_path returns False for local mount."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(
            federation_enabled=True,
            local_box_id="box-a",
        )

        # Mock router
        mock_router = MagicMock()
        mock_route = MagicMock()
        mock_route.mount_point = "/workspace"

        mock_mount = MagicMock()
        mock_mount.backend_config = '{"box_id": "box-a"}'  # Same as local

        mock_router.route.return_value = mock_route
        mock_router.get_mount.return_value = mock_mount
        mixin.router = mock_router

        is_remote, info = mixin._is_remote_path("/workspace/file.txt")

        assert is_remote is False
        assert info is None

    def test_is_remote_path_remote_mount(self) -> None:
        """Test _is_remote_path returns True for remote mount."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(
            federation_enabled=True,
            local_box_id="box-a",
        )

        # Mock router
        mock_router = MagicMock()
        mock_route = MagicMock()
        mock_route.mount_point = "/mnt/remote"

        mock_mount = MagicMock()
        mock_mount.backend_config = '{"box_id": "box-b", "endpoint": "http://box-b:2026"}'

        mock_router.route.return_value = mock_route
        mock_router.get_mount.return_value = mock_mount
        mixin.router = mock_router

        is_remote, info = mixin._is_remote_path("/mnt/remote/file.txt")

        assert is_remote is True
        assert info is not None
        assert info["box_id"] == "box-b"
        assert info["endpoint"] == "http://box-b:2026"

    def test_get_transport_creates_new(self) -> None:
        """Test _get_transport creates new transport for new endpoint."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(
            federation_enabled=True,
            federation_token="test-token",
        )

        with patch("nexus.core.nexus_fs_federation.NexusRPCTransport") as MockTransport:
            mock_transport = MagicMock()
            MockTransport.return_value = mock_transport

            transport = mixin._get_transport("http://box-b:2026")

            MockTransport.assert_called_once_with(
                endpoint="http://box-b:2026",
                auth_token="test-token",
            )
            assert transport == mock_transport

    def test_get_transport_reuses_existing(self) -> None:
        """Test _get_transport reuses existing transport for same endpoint."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(federation_enabled=True)

        with patch("nexus.core.nexus_fs_federation.NexusRPCTransport") as MockTransport:
            mock_transport = MagicMock()
            MockTransport.return_value = mock_transport

            # First call creates
            transport1 = mixin._get_transport("http://box-b:2026")
            # Second call reuses
            transport2 = mixin._get_transport("http://box-b:2026")

            assert MockTransport.call_count == 1
            assert transport1 is transport2

    def test_close_federation_closes_all_transports(self) -> None:
        """Test close_federation closes all transports."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(federation_enabled=True)

        # Add mock transports
        mock_transport1 = MagicMock()
        mock_transport2 = MagicMock()
        mixin._transports = {
            "http://box-b:2026": mock_transport1,
            "http://box-c:2026": mock_transport2,
        }

        mixin.close_federation()

        mock_transport1.close.assert_called_once()
        mock_transport2.close.assert_called_once()
        assert len(mixin._transports) == 0


class TestFederationForwarding:
    """Tests for request forwarding."""

    def test_forward_read_success(self) -> None:
        """Test successful read forwarding."""
        import base64

        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(
            federation_enabled=True,
            federation_token="test-token",
        )

        mock_transport = MagicMock()
        mock_transport.call.return_value = {"content": base64.b64encode(b"hello world").decode()}
        mixin._transports = {"http://box-b:2026": mock_transport}

        result = mixin._forward_read(
            "/mnt/remote/file.txt",
            {"box_id": "box-b", "endpoint": "http://box-b:2026"},
        )

        assert result == b"hello world"
        mock_transport.call.assert_called_once_with(
            "read",
            {"path": "/mnt/remote/file.txt", "return_metadata": False, "parsed": False},
        )

    def test_forward_write_success(self) -> None:
        """Test successful write forwarding."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(
            federation_enabled=True,
            federation_token="test-token",
        )

        mock_transport = MagicMock()
        mock_transport.call.return_value = {"etag": "abc123", "version": 1}
        mixin._transports = {"http://box-b:2026": mock_transport}

        result = mixin._forward_write(
            "/mnt/remote/file.txt",
            b"new content",
            {"box_id": "box-b", "endpoint": "http://box-b:2026"},
        )

        assert result == {"etag": "abc123", "version": 1}
        mock_transport.call.assert_called_once()

    def test_forward_list_success(self) -> None:
        """Test successful list forwarding."""
        from nexus.core.nexus_fs_federation import NexusFSFederationMixin

        mixin = NexusFSFederationMixin()
        mixin._init_federation(federation_enabled=True)

        mock_transport = MagicMock()
        mock_transport.call.return_value = [
            {"name": "file1.txt", "type": "file"},
            {"name": "dir1", "type": "directory"},
        ]
        mixin._transports = {"http://box-b:2026": mock_transport}

        result = mixin._forward_list(
            "/mnt/remote/",
            {"box_id": "box-b", "endpoint": "http://box-b:2026"},
        )

        assert len(result) == 2
        assert result[0]["name"] == "file1.txt"

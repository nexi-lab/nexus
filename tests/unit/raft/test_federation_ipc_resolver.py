"""Unit tests for FederationIPCResolver (#1625, #1665).

Tests the PRE-DISPATCH resolver for remote DT_PIPE/DT_STREAM using mocks
for metastore and gRPC stubs (no real network or Raft needed).
"""

from unittest.mock import MagicMock, patch

from nexus.raft.federation_ipc_resolver import FederationIPCResolver

SELF_ADDR = "10.0.0.1:50051"
REMOTE_ADDR = "10.0.0.2:50051"


def _make_meta(
    backend_name: str,
    *,
    is_pipe: bool = False,
    is_stream: bool = False,
):
    """Create a mock FileMetadata for IPC types."""
    meta = MagicMock()
    meta.backend_name = backend_name
    meta.is_pipe = is_pipe
    meta.is_stream = is_stream
    meta.etag = "ipc-test"
    return meta


def _make_resolver(**kwargs):
    metastore = kwargs.pop("metastore", MagicMock())
    return FederationIPCResolver(
        metastore=metastore,
        self_address=kwargs.pop("self_address", SELF_ADDR),
        **kwargs,
    )


class TestTryReadNonIPC:
    """try_read() returns None for non-IPC paths."""

    def test_no_metadata_returns_none(self):
        metastore = MagicMock()
        metastore.get.return_value = None
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_read("/test/file.txt") is None

    def test_regular_file_returns_none(self):
        meta = _make_meta(f"cas_local@{REMOTE_ADDR}", is_pipe=False, is_stream=False)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_read("/test/file.txt") is None

    def test_empty_backend_name_returns_none(self):
        meta = MagicMock()
        meta.backend_name = ""
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_read("/test/file.txt") is None


class TestTryReadLocalPipe:
    """try_read() returns None for local pipes."""

    def test_local_pipe_returns_none(self):
        meta = _make_meta(f"pipe@{SELF_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_read("/ipc/my-pipe") is None

    def test_legacy_pipe_no_origin_returns_none(self):
        meta = _make_meta("pipe", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_read("/ipc/my-pipe") is None


class TestTryReadLocalStream:
    """try_read() returns None for local streams."""

    def test_local_stream_returns_none(self):
        meta = _make_meta(f"stream@{SELF_ADDR}", is_stream=True)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_read("/ipc/my-stream") is None

    def test_legacy_stream_no_origin_returns_none(self):
        meta = _make_meta("stream", is_stream=True)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_read("/ipc/my-stream") is None


class TestTryReadRemotePipe:
    """try_read() returns data for remote pipes."""

    @patch.object(FederationIPCResolver, "_read_remote")
    def test_remote_pipe_returns_data(self, mock_read):
        mock_read.return_value = b"pipe data"
        meta = _make_meta(f"pipe@{REMOTE_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)

        result = resolver.try_read("/ipc/remote-pipe")
        assert result == b"pipe data"
        mock_read.assert_called_once_with(REMOTE_ADDR, "/ipc/remote-pipe")


class TestTryReadRemoteStream:
    """try_read() returns data for remote streams."""

    @patch.object(FederationIPCResolver, "_read_remote")
    def test_remote_stream_returns_data(self, mock_read):
        mock_read.return_value = b"stream data"
        meta = _make_meta(f"stream@{REMOTE_ADDR}", is_stream=True)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)

        result = resolver.try_read("/ipc/remote-stream")
        assert result == b"stream data"
        mock_read.assert_called_once_with(REMOTE_ADDR, "/ipc/remote-stream")


class TestTryWriteRemote:
    """try_write() sends to remote peer via gRPC Call RPC."""

    @patch.object(FederationIPCResolver, "_write_remote")
    def test_write_remote_pipe(self, mock_write):
        mock_write.return_value = 5
        meta = _make_meta(f"pipe@{REMOTE_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_write("/ipc/remote-pipe", b"hello")

        assert result == {}
        mock_write.assert_called_once_with(REMOTE_ADDR, "/ipc/remote-pipe", b"hello")

    @patch.object(FederationIPCResolver, "_write_remote")
    def test_write_remote_stream_returns_offset(self, mock_write):
        mock_write.return_value = 42
        meta = _make_meta(f"stream@{REMOTE_ADDR}", is_stream=True)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_write("/ipc/remote-stream", b"data")

        assert result == {"offset": 42}
        mock_write.assert_called_once_with(REMOTE_ADDR, "/ipc/remote-stream", b"data")

    def test_write_local_pipe_returns_none(self):
        meta = _make_meta(f"pipe@{SELF_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_write("/ipc/my-pipe", b"data") is None


class TestTryDeleteRemote:
    """try_delete() destroys remote pipe/stream via gRPC Delete RPC."""

    @patch.object(FederationIPCResolver, "_delete_remote")
    def test_delete_remote_pipe(self, mock_delete):
        meta = _make_meta(f"pipe@{REMOTE_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/ipc/remote-pipe")

        assert result == {}
        mock_delete.assert_called_once_with(REMOTE_ADDR, "/ipc/remote-pipe")

    @patch.object(FederationIPCResolver, "_delete_remote")
    def test_delete_remote_stream(self, mock_delete):
        meta = _make_meta(f"stream@{REMOTE_ADDR}", is_stream=True)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/ipc/remote-stream")

        assert result == {}
        mock_delete.assert_called_once_with(REMOTE_ADDR, "/ipc/remote-stream")

    def test_delete_local_returns_none(self):
        meta = _make_meta(f"pipe@{SELF_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta
        resolver = _make_resolver(metastore=metastore)
        assert resolver.try_delete("/ipc/my-pipe") is None


class TestKernelDispatchIntegration:
    """Verify FederationIPCResolver works with KernelDispatch."""

    def test_resolve_read_non_ipc_passes_through(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        meta = _make_meta(f"cas_local@{REMOTE_ADDR}", is_pipe=False, is_stream=False)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_read("/test/file.txt")
        assert handled is False
        assert result is None

    def test_resolve_read_local_pipe_passes_through(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        meta = _make_meta(f"pipe@{SELF_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_read("/ipc/my-pipe")
        assert handled is False
        assert result is None

    @patch.object(FederationIPCResolver, "_read_remote")
    def test_resolve_read_remote_pipe_is_handled(self, mock_read):
        from nexus.core.kernel_dispatch import KernelDispatch

        mock_read.return_value = b"remote pipe data"
        meta = _make_meta(f"pipe@{REMOTE_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_read("/ipc/remote-pipe")
        assert handled is True
        assert result == b"remote pipe data"

    @patch.object(FederationIPCResolver, "_write_remote")
    def test_resolve_write_remote_stream_is_handled(self, mock_write):
        from nexus.core.kernel_dispatch import KernelDispatch

        mock_write.return_value = 99
        meta = _make_meta(f"stream@{REMOTE_ADDR}", is_stream=True)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_write("/ipc/remote-stream", b"data")
        assert handled is True
        assert result == {"offset": 99}

    @patch.object(FederationIPCResolver, "_delete_remote")
    def test_resolve_delete_remote_pipe_is_handled(self, mock_delete):
        from nexus.core.kernel_dispatch import KernelDispatch

        meta = _make_meta(f"pipe@{REMOTE_ADDR}", is_pipe=True)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_delete("/ipc/remote-pipe")
        assert handled is True
        assert result == {}

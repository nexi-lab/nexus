"""Tests for @fuse_operation error mapping decorator."""

from __future__ import annotations

import errno

import pytest
from fuse import FuseOSError

from nexus.core.exceptions import (
    NexusFileNotFoundError,
    NexusPermissionError,
    RemoteConnectionError,
    RemoteTimeoutError,
)
from nexus.fuse.operations import fuse_operation


class _FakeOps:
    """Minimal stand-in so the decorator's `self` arg is satisfied.

    The fuse_operation decorator type-hints self as NexusFUSEOperations but
    does not check at runtime — any object works.
    """

    pass


def _make_decorated(side_effect: Exception | None = None, return_value: object = None):
    """Build a decorated function that raises *side_effect* or returns *return_value*."""

    @fuse_operation("TEST")
    def fn(self, path, *a, **kw):
        if side_effect is not None:
            raise side_effect
        return return_value

    return fn


class TestFuseOperationDecorator:
    """@fuse_operation maps domain exceptions → FuseOSError with correct errno."""

    def test_passthrough_on_success(self):
        fn = _make_decorated(return_value={"st_size": 42})
        result = fn(_FakeOps(), "/file")
        assert result == {"st_size": 42}

    def test_fuse_os_error_passthrough(self):
        fn = _make_decorated(side_effect=FuseOSError(errno.ENOENT))
        with pytest.raises(FuseOSError) as exc_info:
            fn(_FakeOps(), "/file")
        assert exc_info.value.errno == errno.ENOENT

    def test_file_not_found_maps_to_enoent(self):
        fn = _make_decorated(side_effect=NexusFileNotFoundError("/missing"))
        with pytest.raises(FuseOSError) as exc_info:
            fn(_FakeOps(), "/missing")
        assert exc_info.value.errno == errno.ENOENT

    def test_permission_error_maps_to_eacces(self):
        fn = _make_decorated(side_effect=NexusPermissionError("forbidden"))
        with pytest.raises(FuseOSError) as exc_info:
            fn(_FakeOps(), "/secret")
        assert exc_info.value.errno == errno.EACCES

    def test_remote_timeout_maps_to_etimedout(self):
        fn = _make_decorated(side_effect=RemoteTimeoutError("timeout"))
        with pytest.raises(FuseOSError) as exc_info:
            fn(_FakeOps(), "/slow")
        assert exc_info.value.errno == errno.ETIMEDOUT

    def test_remote_connection_maps_to_econnrefused(self):
        fn = _make_decorated(side_effect=RemoteConnectionError("refused"))
        with pytest.raises(FuseOSError) as exc_info:
            fn(_FakeOps(), "/down")
        assert exc_info.value.errno == errno.ECONNREFUSED

    def test_unexpected_exception_maps_to_eio(self):
        fn = _make_decorated(side_effect=RuntimeError("boom"))
        with pytest.raises(FuseOSError) as exc_info:
            fn(_FakeOps(), "/bad")
        assert exc_info.value.errno == errno.EIO

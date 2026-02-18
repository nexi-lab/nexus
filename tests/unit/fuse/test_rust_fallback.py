"""Tests for _try_rust delegation and fallback logic."""

from __future__ import annotations

import errno
from typing import Any
from unittest.mock import MagicMock

import pytest
from fuse import FuseOSError


class TestTryRust:
    """_try_rust: Rust delegation with fallback to Python."""

    def test_rust_success_returns_result(self, fuse_ops: Any) -> None:
        rust_client = MagicMock()
        rust_client.stat.return_value = {"size": 100}
        fuse_ops._rust_client = rust_client
        fuse_ops._use_rust = True
        fuse_ops._context = None  # no namespace → Rust eligible

        ok, result = fuse_ops._try_rust("STAT", "stat", "/file")
        assert ok is True
        assert result == {"size": 100}

    def test_rust_enoent_re_raises(self, fuse_ops: Any) -> None:
        rust_client = MagicMock()
        rust_client.stat.side_effect = OSError(errno.ENOENT, "not found")
        fuse_ops._rust_client = rust_client
        fuse_ops._use_rust = True
        fuse_ops._context = None

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops._try_rust("STAT", "stat", "/missing")
        assert exc_info.value.errno == errno.ENOENT

    def test_rust_other_oserror_falls_back(self, fuse_ops: Any) -> None:
        rust_client = MagicMock()
        rust_client.read.side_effect = OSError(errno.EIO, "io error")
        fuse_ops._rust_client = rust_client
        fuse_ops._use_rust = True
        fuse_ops._context = None

        ok, result = fuse_ops._try_rust("READ", "read", "/file")
        assert ok is False
        assert result is None

    def test_rust_non_os_exception_falls_back(self, fuse_ops: Any) -> None:
        rust_client = MagicMock()
        rust_client.read.side_effect = RuntimeError("unexpected")
        fuse_ops._rust_client = rust_client
        fuse_ops._use_rust = True
        fuse_ops._context = None

        ok, result = fuse_ops._try_rust("READ", "read", "/file")
        assert ok is False
        assert result is None

    def test_rust_not_available_when_context_set(self, fuse_ops_with_context: Any) -> None:
        """Rust is disabled when a namespace context is set."""
        fuse_ops_with_context._rust_client = MagicMock()
        fuse_ops_with_context._use_rust = True

        ok, result = fuse_ops_with_context._try_rust("READ", "read", "/file")
        assert ok is False

    def test_rust_not_available_when_no_client(self, fuse_ops: Any) -> None:
        fuse_ops._use_rust = True
        fuse_ops._rust_client = None

        ok, result = fuse_ops._try_rust("READ", "read", "/file")
        assert ok is False

    def test_rust_available_property(self, fuse_ops: Any) -> None:
        fuse_ops._use_rust = False
        assert fuse_ops._rust_available is False

        fuse_ops._use_rust = True
        fuse_ops._rust_client = MagicMock()
        fuse_ops._context = None
        assert fuse_ops._rust_available is True

"""Shared fixtures for isolation tests."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from nexus.backends.backend import Backend, HandlerStatusResponse
from nexus.core.response import HandlerResponse
from nexus.isolation.config import IsolationConfig


class MockBackend(Backend):
    """Minimal in-memory backend for isolation testing.

    All operations are synchronous and store data in plain dictionaries.
    The class is picklable so it can be instantiated inside workers.
    """

    def __init__(self, *, fail_connect: bool = False) -> None:
        self._store: dict[str, bytes] = {}
        self._refs: dict[str, int] = {}
        self._dirs: set[str] = {"/"}
        self._connected = False
        self._fail_connect = fail_connect

    # ── Properties ──────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "mock"

    @property
    def user_scoped(self) -> bool:
        return False

    @property
    def thread_safe(self) -> bool:
        return True

    # ── Connection ──────────────────────────────────────────────────────
    def connect(self, context: Any = None) -> HandlerStatusResponse:
        if self._fail_connect:
            return HandlerStatusResponse(success=False, error_message="connect refused")
        self._connected = True
        return HandlerStatusResponse(success=True)

    def disconnect(self, context: Any = None) -> None:
        self._connected = False

    # ── CAS ─────────────────────────────────────────────────────────────
    def write_content(self, content: bytes, context: Any = None) -> HandlerResponse[str]:
        h = hashlib.sha256(content).hexdigest()
        self._store[h] = content
        self._refs[h] = self._refs.get(h, 0) + 1
        return HandlerResponse.ok(data=h, backend_name=self.name)

    def read_content(self, content_hash: str, context: Any = None) -> HandlerResponse[bytes]:
        if content_hash not in self._store:
            return HandlerResponse.not_found(path=content_hash, backend_name=self.name)
        return HandlerResponse.ok(data=self._store[content_hash], backend_name=self.name)

    def delete_content(self, content_hash: str, context: Any = None) -> HandlerResponse[None]:
        if content_hash not in self._store:
            return HandlerResponse.not_found(path=content_hash, backend_name=self.name)
        self._refs[content_hash] = max(0, self._refs.get(content_hash, 1) - 1)
        if self._refs[content_hash] == 0:
            del self._store[content_hash]
            del self._refs[content_hash]
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def content_exists(self, content_hash: str, context: Any = None) -> HandlerResponse[bool]:
        return HandlerResponse.ok(data=content_hash in self._store, backend_name=self.name)

    def get_content_size(self, content_hash: str, context: Any = None) -> HandlerResponse[int]:
        if content_hash not in self._store:
            return HandlerResponse.not_found(path=content_hash, backend_name=self.name)
        return HandlerResponse.ok(data=len(self._store[content_hash]), backend_name=self.name)

    def get_ref_count(self, content_hash: str, context: Any = None) -> HandlerResponse[int]:
        return HandlerResponse.ok(data=self._refs.get(content_hash, 0), backend_name=self.name)

    # ── Directories ─────────────────────────────────────────────────────
    def mkdir(
        self, path: str, parents: bool = False, exist_ok: bool = False, context: Any = None
    ) -> HandlerResponse[None]:
        if path in self._dirs and not exist_ok:
            return HandlerResponse.error(f"Directory exists: {path}", code=409)
        self._dirs.add(path)
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def rmdir(
        self, path: str, recursive: bool = False, context: Any = None
    ) -> HandlerResponse[None]:
        if path not in self._dirs:
            return HandlerResponse.not_found(path=path, backend_name=self.name)
        self._dirs.discard(path)
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def is_directory(self, path: str, context: Any = None) -> HandlerResponse[bool]:
        return HandlerResponse.ok(data=path in self._dirs, backend_name=self.name)

    def list_dir(self, path: str, context: Any = None) -> list[str]:
        if path not in self._dirs:
            raise FileNotFoundError(path)
        return []


class FailConnectBackend(MockBackend):
    """Backend that raises on connect() — for testing corrupted worker state."""

    def connect(self, context: Any = None) -> HandlerStatusResponse:
        raise ConnectionError("test: connect refused")


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_backend() -> MockBackend:
    """A fresh MockBackend instance."""
    b = MockBackend()
    b.connect()
    return b


@pytest.fixture()
def mock_config() -> IsolationConfig:
    """IsolationConfig that points to the MockBackend in this module."""
    return IsolationConfig(
        backend_module="tests.unit.isolation.conftest",
        backend_class="MockBackend",
        pool_size=1,
        call_timeout=5.0,
        startup_timeout=5.0,
    )

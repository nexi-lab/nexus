from __future__ import annotations

import pytest
from testkit.backends import (
    DictMetastore,
    FactoryStubBackend,
    FailingBackend,
    InMemoryBackend,
    InMemoryNexusFS,
    InMemoryRecordStore,
)

from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError


def test_in_memory_backend_round_trips_content() -> None:
    backend = InMemoryBackend()
    result = backend.write_content(b"hello")

    assert result.content_id
    assert result.version == result.content_id
    assert result.size == 5
    assert backend.read_content(result.content_id) == b"hello"
    assert backend.content_exists(result.content_id) is True
    assert backend.get_content_size(result.content_id) == 5


def test_in_memory_backend_raises_for_missing_content() -> None:
    backend = InMemoryBackend()

    with pytest.raises(NexusFileNotFoundError):
        backend.read_content("missing")


def test_in_memory_backend_tracks_directories() -> None:
    backend = InMemoryBackend()

    backend.mkdir("/a/b", parents=True, exist_ok=True)

    assert backend.is_directory("/a") is True
    assert backend.is_directory("/a/b") is True
    assert backend.list_dir("/") == ["a/"]
    assert backend.list_dir("/a") == ["b/"]


def test_in_memory_backend_rmdir_rejects_non_empty_directory() -> None:
    backend = InMemoryBackend()
    backend.mkdir("/a/b", parents=True, exist_ok=True)

    with pytest.raises(BackendError):
        backend.rmdir("/a")

    assert backend.is_directory("/a") is True
    assert backend.is_directory("/a/b") is True


def test_in_memory_backend_rmdir_recursive_removes_children() -> None:
    backend = InMemoryBackend()
    backend.mkdir("/a/b", parents=True, exist_ok=True)

    backend.rmdir("/a", recursive=True)

    assert backend.is_directory("/a") is False
    assert backend.is_directory("/a/b") is False


def test_factory_stub_backend_accepts_arbitrary_kwargs() -> None:
    backend = FactoryStubBackend(token="secret")

    assert backend.kwargs == {"token": "secret"}
    assert backend.name == "stub"
    assert backend.has_feature("anything") is False


def test_existing_helpers_are_reexported() -> None:
    assert callable(DictMetastore)
    assert callable(InMemoryNexusFS)
    assert callable(InMemoryRecordStore)
    assert callable(FailingBackend)

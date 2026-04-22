"""Shared test helpers for backend wrapper tests (#2077).

Provides ``make_leaf()`` and ``make_storage_mock()`` factory functions
used by all wrapper test files.  Centralizes mock setup so Backend
interface changes only need updating in one place.
"""

import hashlib
from unittest.mock import MagicMock, PropertyMock

from nexus.backends.base.backend import Backend
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.object_store import WriteResult


def make_leaf(name: str = "local") -> MagicMock:
    """Create a mock leaf backend with standard property stubs.

    Args:
        name: Backend name returned by ``.name`` and ``.describe()``.

    Returns:
        A ``MagicMock`` spec'd to ``Backend`` with all capability
        properties configured to return sensible defaults.
    """
    mock = MagicMock(spec=Backend)
    mock.name = name
    mock.describe.return_value = name
    type(mock).is_connected = PropertyMock(return_value=True)
    type(mock).thread_safe = PropertyMock(return_value=True)
    type(mock).supports_rename = PropertyMock(return_value=False)
    type(mock).has_root_path = PropertyMock(return_value=True)
    type(mock).has_data_dir = PropertyMock(return_value=False)
    type(mock).supports_parallel_mmap_read = PropertyMock(return_value=False)
    return mock


def make_storage_mock() -> tuple[MagicMock, dict[str, bytes]]:
    """Create a mock leaf that stores/retrieves content in-memory.

    Returns:
        Tuple of ``(mock, storage_dict)`` where ``storage_dict``
        maps content hash (SHA-256 hex) to bytes.
    """
    storage: dict[str, bytes] = {}
    mock = make_leaf("storage-mock")

    def write_content(
        content: bytes, content_id: str = "", *, offset: int = 0, context: object = None
    ) -> WriteResult:
        h = hashlib.sha256(content).hexdigest()
        storage[h] = content
        return WriteResult(content_id=h, version=h, size=len(content))

    def read_content(content_hash: str, context: object = None) -> bytes:
        if content_hash in storage:
            return storage[content_hash]
        raise NexusFileNotFoundError(content_hash)

    def batch_read_content(
        content_hashes: list[str],
        context: object = None,
        *,
        contexts: dict | None = None,
    ) -> dict[str, bytes | None]:
        return {h: storage.get(h) for h in content_hashes}

    mock.write_content = MagicMock(side_effect=write_content)
    mock.read_content = MagicMock(side_effect=read_content)
    mock.batch_read_content = MagicMock(side_effect=batch_read_content)
    mock.delete_content = MagicMock(return_value=None)
    return mock, storage

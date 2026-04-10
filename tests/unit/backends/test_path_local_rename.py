from pathlib import Path

import pytest

from nexus.backends.storage.path_local import PathLocalBackend
from nexus.factory.orchestrator import create_nexus_fs
from tests.helpers.dict_metastore import DictMetastore

_LARGE_CONTENT = b"x" * 100


@pytest.mark.asyncio
async def test_directory_rename_path_local(tmp_path: Path):
    """Verify that renaming a directory updates metadata for PathLocalBackend.

    Rename is a metadata-only operation — physical files stay in place,
    only virtual path mappings are updated in the metastore.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    backend = PathLocalBackend(root_path=data_dir)
    from nexus.core.config import PermissionConfig

    nx = await create_nexus_fs(
        backend=backend,
        metadata_store=DictMetastore(),
        record_store=None,
        permissions=PermissionConfig(enforce=False),
    )

    # Create a directory and a file inside it (large content to ensure backend storage)
    nx.mkdir("/old_dir")
    nx.write("/old_dir/test.txt", _LARGE_CONTENT)

    # Rename the directory
    nx.sys_rename("/old_dir", "/new_dir")

    # Check metadata — virtual paths should be updated
    assert nx.access("/new_dir")
    assert nx.access("/new_dir/test.txt")
    assert not nx.access("/old_dir")


if __name__ == "__main__":
    pytest.main([__file__])

from pathlib import Path

import pytest

from nexus.backends.storage.path_local import PathLocalBackend
from nexus.factory.orchestrator import create_nexus_fs
from tests.helpers.dict_metastore import DictMetastore

_LARGE_CONTENT = b"x" * 100


@pytest.mark.asyncio
async def test_directory_rename_path_local(tmp_path: Path):
    """Verify that renaming a directory also renames it in the physical storage for PathLocalBackend."""
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
    await nx.mkdir("/old_dir")
    await nx.write("/old_dir/test.txt", _LARGE_CONTENT)

    # Check physical existence
    assert (data_dir / "old_dir").is_dir()
    assert (data_dir / "old_dir" / "test.txt").is_file()

    # Rename the directory
    await nx.sys_rename("/old_dir", "/new_dir")

    # Check metadata
    assert await nx.sys_access("/new_dir")
    assert await nx.sys_access("/new_dir/test.txt")
    assert not await nx.sys_access("/old_dir")

    # Check physical existence — THIS IS WHAT WE WANT TO VERIFY
    assert (data_dir / "new_dir").is_dir()
    assert (data_dir / "new_dir" / "test.txt").is_file()
    assert not (data_dir / "old_dir").exists()


if __name__ == "__main__":
    pytest.main([__file__])

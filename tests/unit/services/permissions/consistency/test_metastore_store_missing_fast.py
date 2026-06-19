from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.consistency.metastore_version_store import MetastoreVersionStore


class MissingStatFilesystem:
    def __init__(self) -> None:
        self.read_calls = 0

    def sys_stat(self, path: str, **kwargs):  # noqa: ANN001, ARG002
        return None

    def sys_read(self, path: str, **kwargs):  # noqa: ANN001, ARG002
        self.read_calls += 1
        raise AssertionError("missing metastore files should not be read")


def test_namespace_store_uses_stat_to_avoid_missing_read() -> None:
    fs = MissingStatFilesystem()
    store = MetastoreNamespaceStore(fs)

    assert store.get("file") is None
    assert fs.read_calls == 0


def test_version_store_uses_stat_to_avoid_missing_read() -> None:
    fs = MissingStatFilesystem()
    store = MetastoreVersionStore(fs)

    assert store.get_version("zone-a") == 0
    assert fs.read_calls == 0

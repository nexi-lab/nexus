"""fsspec upstream compliance tests.

Runs the official fsspec abstract test suite against NexusFileSystem
to validate compatibility with the fsspec contract.

Known deviations are marked with xfail + reason so they serve as
living documentation of where we diverge from the full fsspec spec.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

import pytest

# Guard: fsspec abstract tests are in the fsspec package itself
HAS_ABSTRACT_TESTS = False
_AbstractFixturesBase: type = object
try:
    from fsspec.tests.abstract import AbstractFixtures as _AbstractFixturesBase

    HAS_ABSTRACT_TESTS = True
except ImportError:
    pass
AbstractFixtures = _AbstractFixturesBase


pytest.importorskip("fsspec", reason="fsspec required for upstream compliance tests")

from nexus.contracts.constants import ROOT_ZONE_ID  # noqa: E402
from nexus.contracts.metadata import DT_MOUNT  # noqa: E402
from nexus.contracts.types import OperationContext  # noqa: E402
from nexus.core.config import PermissionConfig  # noqa: E402
from nexus.core.nexus_fs import NexusFS  # noqa: E402
from nexus.fs import _make_mount_entry  # noqa: E402
from nexus.fs._fsspec import NexusFileSystem  # noqa: E402
from nexus.fs._sqlite_meta import SQLiteMetastore  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_ABSTRACT_TESTS,
    reason="fsspec abstract test suite not available (install fsspec[test])",
)


def _build_nexus_fsspec(tmp_path: Path) -> NexusFileSystem:
    """Build a NexusFileSystem backed by a real local CASLocalBackend."""
    from nexus.backends.storage.cas_local import CASLocalBackend

    db_path = str(tmp_path / "metadata.db")
    metastore = SQLiteMetastore(db_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backend = CASLocalBackend(root_path=data_dir)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )
    kernel.sys_setattr("/local", entry_type=DT_MOUNT, backend=backend)
    metastore.put(_make_mount_entry("/local", backend.name))
    kernel._init_cred = OperationContext(
        user_id="test",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )
    return NexusFileSystem(nexus_fs=kernel)


class NexusFixtures(AbstractFixtures):
    """Provide fixtures required by fsspec abstract tests."""

    @pytest.fixture
    def fs(self, tmp_path):
        return _build_nexus_fsspec(tmp_path)

    @pytest.fixture
    def fs_path(self):
        return "/local"

    @pytest.fixture
    def fs_join(self):
        return posixpath.join

    @pytest.fixture
    def supports_empty_directories(self):
        return True


# ── Abstract test suites ─────────────────────────────────────────────────
# Each class runs the upstream test suite for a specific operation category.
# Marked xfail(strict=False) so they document compliance gaps without
# blocking CI. As we fix gaps, tests will start passing and the xfail
# will be silently ignored (strict=False).
#
# Known deviation categories:
# - put/get: require local-to-remote and remote-to-local transfer methods
#   that NexusFileSystem doesn't implement yet
# - glob: our ls/info doesn't fully support glob patterns
# - copy directory: requires recursive directory copy support
# - open append/r+: explicitly unsupported (documented in _fsspec.py)

_upstream_xfail = pytest.mark.xfail(
    reason="Upstream fsspec abstract test — compliance gap documented for v0.1.0",
    strict=False,
)

try:
    from fsspec.tests.abstract import AbstractCopyTests

    @_upstream_xfail
    class TestNexusCopy(NexusFixtures, AbstractCopyTests):
        pass
except ImportError:
    pass

try:
    from fsspec.tests.abstract import AbstractPutTests

    @_upstream_xfail
    class TestNexusPut(NexusFixtures, AbstractPutTests):
        pass
except ImportError:
    pass

try:
    from fsspec.tests.abstract import AbstractGetTests

    @_upstream_xfail
    class TestNexusGet(NexusFixtures, AbstractGetTests):
        pass
except ImportError:
    pass

try:
    from fsspec.tests.abstract import AbstractPipeTests

    @_upstream_xfail
    class TestNexusPipe(NexusFixtures, AbstractPipeTests):
        pass
except ImportError:
    pass

try:
    from fsspec.tests.abstract import AbstractOpenTests

    @_upstream_xfail
    class TestNexusOpen(NexusFixtures, AbstractOpenTests):
        pass
except ImportError:
    pass


# ── Manual compliance tests (always run) ─────────────────────────────────


class TestFsspecContractManual:
    """Manual compliance tests that always run regardless of fsspec version."""

    @pytest.fixture
    def nfs(self, tmp_path):
        return _build_nexus_fsspec(tmp_path)

    def test_protocol_registered(self, nfs):
        assert nfs.protocol == ("nexus",)

    def test_strip_protocol(self, nfs):
        assert nfs._strip_protocol("nexus:///local/file.txt") == "/local/file.txt"
        assert nfs._strip_protocol("nexus://local/file.txt") == "/local/file.txt"
        assert nfs._strip_protocol("/local/file.txt") == "/local/file.txt"

    def test_ls_returns_list(self, nfs):
        nfs._pipe_file("/local/ls_test.txt", b"data")
        entries = nfs.ls("/local", detail=True)
        assert isinstance(entries, list)
        assert len(entries) >= 1
        # Each entry must have name, size, type
        for entry in entries:
            assert "name" in entry
            assert "size" in entry
            assert "type" in entry
            assert entry["type"] in ("file", "directory")

    def test_info_returns_dict(self, nfs):
        nfs._pipe_file("/local/info_test.txt", b"info data")
        info = nfs.info("/local/info_test.txt")
        assert isinstance(info, dict)
        assert info["name"] == "/local/info_test.txt"
        assert info["type"] == "file"
        assert info["size"] == 9

    def test_cat_file_reads_content(self, nfs):
        nfs._pipe_file("/local/cat_test.txt", b"cat content")
        result = nfs._cat_file("/local/cat_test.txt")
        assert result == b"cat content"

    def test_cat_file_byte_range(self, nfs):
        nfs._pipe_file("/local/range_test.txt", b"0123456789")
        result = nfs._cat_file("/local/range_test.txt", start=2, end=7)
        assert result == b"23456"

    def test_pipe_and_cat_roundtrip(self, nfs):
        data = b"roundtrip data"
        nfs._pipe_file("/local/roundtrip.txt", data)
        assert nfs._cat_file("/local/roundtrip.txt") == data

    def test_mkdir(self, nfs):
        nfs._mkdir("/local/test_dir", create_parents=True)
        info = nfs.info("/local/test_dir")
        assert info["type"] == "directory"

    def test_rm_file(self, nfs):
        nfs._pipe_file("/local/rm_test.txt", b"delete me")
        nfs._rm("/local/rm_test.txt")
        with pytest.raises(FileNotFoundError):
            nfs.info("/local/rm_test.txt")

    def test_cp_file(self, nfs):
        nfs._pipe_file("/local/cp_src.txt", b"copy data")
        nfs._cp_file("/local/cp_src.txt", "/local/cp_dst.txt")
        assert nfs._cat_file("/local/cp_dst.txt") == b"copy data"

    def test_open_read(self, nfs):
        nfs._pipe_file("/local/open_read.txt", b"readable")
        with nfs._open("/local/open_read.txt", "rb") as f:
            assert f.read() == b"readable"

    def test_open_write(self, nfs):
        with nfs._open("/local/open_write.txt", "wb") as f:
            f.write(b"written")
        assert nfs._cat_file("/local/open_write.txt") == b"written"

    def test_unsupported_append_mode(self, nfs):
        with pytest.raises(ValueError, match="Unsupported mode"):
            nfs._open("/local/append.txt", "a")

    def test_file_not_found(self, nfs):
        with pytest.raises(FileNotFoundError):
            nfs.info("/local/nonexistent.txt")

    def test_dircache_populated(self, nfs):
        nfs._pipe_file("/local/cache_test.txt", b"cache")
        # First call populates cache
        nfs.ls("/local", detail=True)
        assert "/local" in nfs.dircache
        # Second call uses cache (no error even if backend is slow)
        cached = nfs.ls("/local", detail=True)
        assert any(e["name"] == "/local/cache_test.txt" for e in cached)

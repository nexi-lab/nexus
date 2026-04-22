"""fsspec upstream abstract test suite for NexusFileSystem.

Runs the canonical conformance tests from ``fsspec.tests.abstract`` against a
real NexusFileSystem backed by CASLocalBackend, following the same pattern used
by s3fs and gcsfs.
"""

from __future__ import annotations

import pytest

fsspec = pytest.importorskip("fsspec")

from fsspec.tests import abstract  # noqa: E402

from nexus.backends.storage.cas_local import CASLocalBackend  # noqa: E402
from nexus.contracts.constants import ROOT_ZONE_ID  # noqa: E402
from nexus.contracts.metadata import DT_MOUNT  # noqa: E402
from nexus.contracts.types import OperationContext  # noqa: E402
from nexus.core.config import PermissionConfig  # noqa: E402
from nexus.core.nexus_fs import NexusFS  # noqa: E402
from nexus.fs import _make_mount_entry  # noqa: E402
from nexus.fs._facade import SlimNexusFS  # noqa: E402
from nexus.fs._fsspec import NexusFileSystem  # noqa: E402
from nexus.fs._sqlite_meta import SQLiteMetastore  # noqa: E402

# ---------------------------------------------------------------------------
# Thin subclass that bridges NexusFileSystem gaps for the abstract suite.
#
# Several base ``AbstractFileSystem`` methods are no-ops (``mkdir``,
# ``makedirs``) or raise ``NotImplementedError`` (``cp_file``).
# NexusFileSystem implements ``_``-prefixed variants but the installed
# fsspec version does not auto-delegate from the public API to them.
#
# This subclass also ensures:
# - Parent directories are explicitly created before writes/copies so that
#   the metastore-backed ``ls()`` can discover them.
# - The ``dircache`` is cleared after every mutation to avoid stale listings.
# - ``rm`` handles recursive directory deletion correctly by using the
#   kernel's ``rmdir(recursive=True)`` for any directory it encounters.
# ---------------------------------------------------------------------------


class _ConformanceFS(NexusFileSystem):
    """NexusFileSystem with public API wired up for conformance tests.

    The base fsspec ``AbstractFileSystem`` has no-op ``mkdir``/``makedirs``
    and raises ``NotImplementedError`` from ``cp_file``.  NexusFileSystem
    implements the ``_``-prefixed variants (``_mkdir``, ``_cp_file``) but
    the installed fsspec version does not auto-delegate to them.  This thin
    subclass bridges the gap so the abstract test suite sees working
    implementations.

    Every mutating method invalidates the dircache so that ``ls``/``info``
    always reflect the true filesystem state.
    """

    def mkdir(self, path, create_parents=True, **kwargs):
        self._mkdir(path, create_parents=create_parents, **kwargs)
        self.dircache.clear()

    def makedirs(self, path, exist_ok=False):
        if not exist_ok and self.isdir(path):
            raise FileExistsError(path)
        self._mkdir(path, create_parents=True)
        self.dircache.clear()

    def cp_file(self, path1, path2, **kwargs):
        import posixpath

        # The base copy() expands directories when recursive=True but then
        # calls cp_file on every entry including directories.  NexusFS.copy
        # only handles files, so we create destination directories ourselves.
        path1 = self._strip_protocol(path1)
        path2 = self._strip_protocol(path2)
        if self.isdir(path1):
            self.mkdir(path2)
        else:
            # Ensure the parent directory of the destination exists and is
            # registered in the metastore so that ls() can find it.
            parent = posixpath.dirname(path2)
            if parent and not self.isdir(parent):
                self.makedirs(parent, exist_ok=True)
            self._cp_file(path1, path2, **kwargs)
        self.dircache.clear()

    def pipe_file(self, path, value, mode="overwrite", **kwargs):
        import posixpath

        path_clean = self._strip_protocol(path)
        parent = posixpath.dirname(path_clean)
        if parent and not self.isdir(parent):
            self.makedirs(parent, exist_ok=True)
        super().pipe_file(path, value, mode=mode, **kwargs)
        self.dircache.clear()

    def touch(self, path, truncate=True, **kwargs):
        import posixpath

        path_clean = self._strip_protocol(path)
        parent = posixpath.dirname(path_clean)
        if parent and not self.isdir(parent):
            self.makedirs(parent, exist_ok=True)
        super().touch(path, truncate=truncate, **kwargs)
        self.dircache.clear()

    def rm(self, path, recursive=False, maxdepth=None):
        self.dircache.clear()
        # Expand paths first, then process in deepest-first order.
        # We need to handle directories ourselves because the base
        # rm -> rm_file -> _rm chain does not pass recursive=True.
        paths = self.expand_path(path, recursive=recursive, maxdepth=maxdepth)
        for p in reversed(paths):
            try:
                info = self.info(p)
            except FileNotFoundError:
                continue
            if info["type"] == "directory":
                # Use recursive=True for directories because NexusFS's
                # metastore may contain implicit children that ls/find
                # didn't discover (e.g. files created by sys_copy).
                self._rm(p, recursive=True)
            else:
                self._rm(p)
        self.dircache.clear()


class NexusFsFixtures(abstract.AbstractFixtures):
    """Fixture overrides that wire up NexusFileSystem with a CASLocalBackend."""

    @pytest.fixture
    def fs(self, tmp_path):
        """Create a real NexusFileSystem (conformance subclass) backed by CASLocalBackend."""
        _ConformanceFS.clear_instance_cache()

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

        facade = SlimNexusFS(kernel)
        nfs = _ConformanceFS(nexus_fs=facade)
        yield nfs
        nfs._runner.close()
        _ConformanceFS.clear_instance_cache()

    @pytest.fixture
    def fs_join(self):
        """Use posixpath.join -- NexusFS paths are always forward-slash."""
        import posixpath

        return posixpath.join

    @pytest.fixture
    def fs_path(self):
        """Return the mount prefix under which test files are created."""
        return "/local"

    @pytest.fixture
    def supports_empty_directories(self):
        """CAS local backend supports empty directories."""
        return True


# ---------------------------------------------------------------------------
# Concrete test classes via multiple inheritance
# ---------------------------------------------------------------------------


class TestNexusCopy(abstract.AbstractCopyTests, NexusFsFixtures):
    pass


class TestNexusGet(abstract.AbstractGetTests, NexusFsFixtures):
    pass


class TestNexusPut(abstract.AbstractPutTests, NexusFsFixtures):
    pass


class TestNexusOpen(abstract.AbstractOpenTests, NexusFsFixtures):
    pass


class TestNexusPipe(abstract.AbstractPipeTests, NexusFsFixtures):
    pass

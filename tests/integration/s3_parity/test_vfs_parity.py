"""S3-vs-local VFS parity (#4267)."""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestVfsParity:
    def test_write_read_parity_smoke(self, parity_kernel):
        h = parity_kernel
        local_p, s3_p = h.paths("smoke.txt")
        content = b"parity!"
        h.fs.write(local_p, content, context=h.ctx)
        h.fs.write(s3_p, content, context=h.ctx)
        assert h.fs.sys_read(local_p, context=h.ctx) == content
        assert h.fs.sys_read(s3_p, context=h.ctx) == content

    def test_overwrite_parity(self, parity_kernel):
        h = parity_kernel
        for p in h.paths("ow.txt"):
            h.fs.write(p, b"v1", context=h.ctx)
            h.fs.write(p, b"v2", context=h.ctx)
            assert h.fs.sys_read(p, context=h.ctx) == b"v2"

    def test_binary_and_empty_parity(self, parity_kernel):
        h = parity_kernel
        blob = bytes(range(256))
        for rel, payload in [("bin.dat", blob), ("empty.txt", b"")]:
            for p in h.paths(rel):
                h.fs.write(p, payload, context=h.ctx)
                assert h.fs.sys_read(p, context=h.ctx) == payload

    def test_stat_parity(self, parity_kernel):
        # NOTE: mtime is excluded — local FS mtime vs S3 LastModified differ by
        # construction (documented-acceptable, #4267 §6). Compare size + is_directory.
        h = parity_kernel
        local_p, s3_p = h.paths("meta.txt")
        h.fs.write(local_p, b"thirteen byte", context=h.ctx)
        h.fs.write(s3_p, b"thirteen byte", context=h.ctx)
        ls = h.fs.sys_stat(local_p, context=h.ctx)
        ss = h.fs.sys_stat(s3_p, context=h.ctx)
        assert ls["size"] == ss["size"] == 13
        assert ls["is_directory"] is ss["is_directory"] is False

    def test_delete_parity(self, parity_kernel):
        h = parity_kernel
        for p in h.paths("del.txt"):
            h.fs.write(p, b"bye", context=h.ctx)
            h.fs.sys_unlink(p, context=h.ctx)
            assert h.fs.sys_stat(p, context=h.ctx) is None

    def test_mkdir_parity(self, parity_kernel):
        h = parity_kernel
        for p in h.paths("subdir"):
            h.fs.mkdir(p, parents=True, exist_ok=True, context=h.ctx)
            st = h.fs.sys_stat(p, context=h.ctx)
            assert st is not None and st["is_directory"] is True

    def test_rmdir_parity(self, parity_kernel):
        h = parity_kernel
        for p in h.paths("rmme"):
            h.fs.mkdir(p, parents=True, exist_ok=True, context=h.ctx)
            h.fs.rmdir(p, recursive=True, context=h.ctx)
            assert h.fs.sys_stat(p, context=h.ctx) is None

    def test_list_parity(self, parity_kernel):
        h = parity_kernel
        for mp in (h.local_mp, h.s3_mp):
            h.fs.write(f"{mp}/list/a.txt", b"a", context=h.ctx)
            h.fs.write(f"{mp}/list/b.txt", b"b", context=h.ctx)
            entries = list(
                h.fs.sys_readdir(f"{mp}/list/", recursive=True, details=False, context=h.ctx)
            )
            txt = sorted(e.rsplit("/", 1)[-1] for e in entries if e.endswith(".txt"))
            assert txt == ["a.txt", "b.txt"]

    def test_copy_parity(self, parity_kernel):
        h = parity_kernel
        for mp in (h.local_mp, h.s3_mp):
            h.fs.write(f"{mp}/src.txt", b"copy me", context=h.ctx)
            h.fs.sys_copy(f"{mp}/src.txt", f"{mp}/dst.txt", context=h.ctx)
            assert h.fs.sys_read(f"{mp}/dst.txt", context=h.ctx) == b"copy me"

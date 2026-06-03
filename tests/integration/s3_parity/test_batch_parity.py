"""S3-vs-local batch parity + read_bulk resolution (#4267)."""

from __future__ import annotations

import unittest.mock as mock

import pytest


@pytest.mark.integration
class TestBatchParity:
    """Batch-op correctness parity and read_bulk implementation resolution."""

    def test_read_bulk_parity(self, parity_kernel):
        """read_bulk results are byte-identical across local and S3 backends."""
        h = parity_kernel
        rels = [f"bulk_{i}.txt" for i in range(8)]  # >4 → large-batch path
        local_paths, s3_paths = [], []
        for i, rel in enumerate(rels):
            lp, sp = h.paths(rel)
            h.fs.write(lp, f"local-{i}".encode(), context=h.ctx)
            h.fs.write(sp, f"local-{i}".encode(), context=h.ctx)
            local_paths.append(lp)
            s3_paths.append(sp)

        lres = h.fs.read_bulk(local_paths, context=h.ctx)
        sres = h.fs.read_bulk(s3_paths, context=h.ctx)

        # Normalise mount-prefix so paths compare equally.
        lnorm = {k.replace(h.local_mp, ""): v for k, v in lres.items()}
        snorm = {k.replace(h.s3_mp, ""): v for k, v in sres.items()}

        assert lnorm == snorm
        assert lnorm["/bulk_3.txt"] == b"local-3"

    def test_write_batch_parity(self, parity_kernel):
        """write_batch round-trips correctly on both local and S3 backends."""
        h = parity_kernel
        for mp in (h.local_mp, h.s3_mp):
            files = [(f"{mp}/wb_{i}.txt", f"wb-{i}".encode()) for i in range(6)]
            results = h.fs.write_batch(files, context=h.ctx)
            assert len(results) == 6
            for path, payload in files:
                assert h.fs.sys_read(path, context=h.ctx) == payload

    def test_read_bulk_is_sequential_not_batched(self, parity_kernel):
        """RESOLUTION (#4267): read_bulk loops a per-file kernel read; it does NOT
        issue one batched RPC, and (since S3 I/O is Rust-served) it never touches
        the Python backend's batch_read_content either. Performance gap, not a
        correctness gap — results are identical to local.

        Implementation detail (nexus_fs_content.py):
          - Small-batch path (<=4 paths, lines ~293-294): loops paths, calls
            self._kernel.sys_read(path, _rust_ctx) once per file.
          - Large-batch path (>4 paths, lines ~368-369): loops allowed_set, calls
            self._kernel.sys_read(path, _rust_ctx) once per file.

        The spy seam is h.fs._kernel.sys_read (KernelClient.sys_read, a plain
        Python method — mock.patch.object works without any Rust-level hooks).

        Follow-up: https://github.com/nexi-lab/nexus/issues/4307.
        """
        h = parity_kernel
        s3_paths = []
        for i in range(8):  # >4 to exercise the large-batch code path
            _, sp = h.paths(f"seq_{i}.txt")
            h.fs.write(sp, b"x", context=h.ctx)
            s3_paths.append(sp)

        kernel = h.fs._kernel  # KernelClient instance
        orig = kernel.sys_read
        calls: list[str] = []

        def counting(path, *a, **k):
            calls.append(path)
            return orig(path, *a, **k)

        with mock.patch.object(kernel, "sys_read", side_effect=counting):
            result = h.fs.read_bulk(s3_paths, context=h.ctx)

        # Every file must have been read successfully.
        assert all(v == b"x" for v in result.values())
        # One kernel.sys_read call per file — sequential, not a single batched RPC.
        assert len(calls) == len(s3_paths), (
            f"Expected {len(s3_paths)} sequential sys_read calls, got {len(calls)}. "
            "If this fails, read_bulk may have been changed to issue a true batched RPC "
            "(good!), and this test should be updated to assert count == 1 instead."
        )

"""S3-vs-local batch parity + read_bulk batching (#4267 / #4307)."""

from __future__ import annotations

import unittest.mock as mock
from typing import Any

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

    def test_read_bulk_uses_batched_kernel_read(self, parity_kernel):
        """RESOLUTION (#4307 FIXED): read_bulk's large-batch path now issues a
        SINGLE parallel ``kernel.read_batch`` RPC (Rust rayon fan-out) for all
        files instead of N sequential ``sys_read`` calls — and the Rust kernel
        services S3 connector mounts in that batch too, so no per-file fallback
        is needed here. Results stay byte-identical to local (test_read_bulk_parity).
        """
        h = parity_kernel
        s3_paths = []
        for i in range(8):  # >4 to exercise the large-batch code path
            _, sp = h.paths(f"seq_{i}.txt")
            h.fs.write(sp, b"x", context=h.ctx)
            s3_paths.append(sp)

        kernel = h.fs._kernel  # KernelClient instance
        orig_batch = kernel.read_batch
        orig_read = kernel.sys_read
        batch_calls: list[Any] = []
        read_calls: list[str] = []

        def counting_batch(items, *a, **k):
            batch_calls.append(items)
            return orig_batch(items, *a, **k)

        def counting_read(path, *a, **k):
            read_calls.append(path)
            return orig_read(path, *a, **k)

        with (
            mock.patch.object(kernel, "read_batch", side_effect=counting_batch),
            mock.patch.object(kernel, "sys_read", side_effect=counting_read),
        ):
            result = h.fs.read_bulk(s3_paths, context=h.ctx)

        assert all(v == b"x" for v in result.values())
        # Exactly ONE batched kernel RPC carrying all files...
        assert len(batch_calls) == 1, f"expected 1 batched read, got {len(batch_calls)}"
        assert len(batch_calls[0]) == len(s3_paths)
        # ...and no per-file sequential sys_read fallback was needed.
        assert read_calls == [], f"unexpected per-file sys_read fallback: {read_calls}"

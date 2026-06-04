"""S3-vs-local range-read parity (#4267).

Efficiency (whether S3 issues an HTTP Range request vs reads the whole object)
is OUT OF SCOPE per #4266/#4267 §6 — only byte-correctness is asserted here.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestRangeParity:
    def test_read_range_correctness_parity(self, parity_kernel):
        h = parity_kernel
        # 64 KiB deterministic payload so range slices are unambiguous.
        payload = bytes(i % 256 for i in range(64 * 1024))
        local_p, s3_p = h.paths("big.dat")
        h.fs.write(local_p, payload, context=h.ctx)
        h.fs.write(s3_p, payload, context=h.ctx)

        for start, end in [(0, 10), (1000, 2048), (60000, 65536), (65530, 65536)]:
            expected = payload[start:end]
            assert h.fs.read_range(local_p, start, end, context=h.ctx) == expected
            assert h.fs.read_range(s3_p, start, end, context=h.ctx) == expected

    def test_read_range_via_sys_read_offset_parity(self, parity_kernel):
        h = parity_kernel
        payload = bytes(i % 256 for i in range(4096))
        local_p, s3_p = h.paths("offset.dat")
        h.fs.write(local_p, payload, context=h.ctx)
        h.fs.write(s3_p, payload, context=h.ctx)
        # pread(2)-style offset+count must match on both backends.
        assert h.fs.sys_read(local_p, offset=100, count=50, context=h.ctx) == payload[100:150]
        assert h.fs.sys_read(s3_p, offset=100, count=50, context=h.ctx) == payload[100:150]

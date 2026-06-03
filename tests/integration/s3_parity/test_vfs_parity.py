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

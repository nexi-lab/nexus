"""Memory benchmark for SANDBOX profile (Issue #3778).

Gated behind `pytest -m sandbox_memory` — skipped by default because
RSS sampling is flaky on shared CI runners.

Target: < 300 MB RSS after booting + indexing 100 small files.
"""

import asyncio
from pathlib import Path

import psutil
import pytest

import nexus


@pytest.mark.sandbox_memory
@pytest.mark.asyncio
async def test_sandbox_idle_rss_under_300mb(tmp_path: Path) -> None:
    nx = await nexus.connect(config={"profile": "sandbox", "data_dir": str(tmp_path / "nexus")})
    try:
        for i in range(100):
            nx.write(f"/file-{i:03d}.txt", f"content {i} — keyword{i % 7}".encode())

        # Let background tasks settle
        await asyncio.sleep(1.0)

        rss_bytes = psutil.Process().memory_info().rss
        rss_mb = rss_bytes / 1024 / 1024
        print(f"SANDBOX idle RSS: {rss_mb:.1f} MB")
        assert rss_mb < 300, f"RSS {rss_mb:.1f}MB exceeds 300MB target"
    finally:
        nx.close()

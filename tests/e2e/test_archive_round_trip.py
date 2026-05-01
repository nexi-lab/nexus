"""E2E: docker stack ingest → archive create → tear down → restore on fresh stack.

Marked with @pytest.mark.e2e — only runs when an environment variable
NEXUS_E2E=1 is set, since it spins docker.
"""

import os
import subprocess

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(os.environ.get("NEXUS_E2E") != "1", reason="set NEXUS_E2E=1 to run")
def test_e2e_round_trip(tmp_path):
    # 1. Boot lightweight stack (uses nexus-stack.yml or nexus up CLI)
    subprocess.run(["nexus", "up", "--profile", "lightweight"], check=True, timeout=300)

    # 2. Ingest known corpus
    subprocess.run(
        ["nexus", "ingest", "--zone", "eng", "tests/fixtures/archive_corpus_small/"],
        check=True,
    )

    # 3. Create archive
    archive_path = tmp_path / "e2e.nexus"
    subprocess.run(
        ["nexus", "archive", "create", "--zone", "eng", "--output", str(archive_path)],
        check=True,
    )
    assert archive_path.exists()

    # 4. Capture baseline search
    baseline = subprocess.run(
        ["nexus", "search", "--zone", "eng", "--json", "known fixture phrase"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # 5. Tear down
    subprocess.run(["nexus", "down"], check=True)

    # 6. Restore on fresh stack
    subprocess.run(["nexus", "up", "--profile", "lightweight"], check=True, timeout=300)
    subprocess.run(
        ["nexus", "archive", "restore", str(archive_path), "--target-zone", "eng", "--force"],
        check=True,
    )

    # 7. Compare search
    restored = subprocess.run(
        ["nexus", "search", "--zone", "eng", "--json", "known fixture phrase"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert restored == baseline, "search results not byte-identical after restore"

    subprocess.run(["nexus", "down"], check=True)

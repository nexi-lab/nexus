"""E2E: CLI archive create -> restore across fresh local workspaces.

Runs only when ``NEXUS_E2E=1`` because it spawns the local kernel subprocess
through the public CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _kernel_binary_available(repo_root: Path) -> bool:
    return bool(
        shutil.which("nexus-cluster")
        or shutil.which("nexusd-cluster")
        or (repo_root / "target" / "release" / "nexus-cluster").exists()
        or (repo_root / "target" / "release" / "nexusd-cluster").exists()
    )


def _cli_env(repo_root: Path, tmp_path: Path, data_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["NEXUS_DATA_DIR"] = str(data_dir)
    env["NEXUS_PROFILE"] = "local"
    env["PATH"] = f"{repo_root / 'target' / 'release'}{os.pathsep}{env.get('PATH', '')}"
    env.pop("NEXUS_URL", None)
    env.pop("NEXUS_API_KEY", None)
    return env


def _run_cli(
    repo_root: Path,
    env: dict[str, str],
    *args: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nexus.cli", *args],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout,
    )


@pytest.mark.skipif(os.environ.get("NEXUS_E2E") != "1", reason="set NEXUS_E2E=1 to run")
def test_e2e_round_trip(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    if not _kernel_binary_available(repo_root):
        pytest.skip("archive CLI E2E requires nexus-cluster/nexusd-cluster on PATH")

    (tmp_path / "home").mkdir()
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    source_env = _cli_env(repo_root, tmp_path, tmp_path / "source")
    target_env = _cli_env(repo_root, tmp_path, tmp_path / "target")

    _run_cli(
        repo_root,
        source_env,
        "write",
        "/eng/readme.md",
        "known fixture phrase",
    )
    _run_cli(
        repo_root,
        source_env,
        "archive",
        "create",
        "--zone",
        "root",
        "--output",
        str(archive_dir),
        "--no-strip",
    )

    bundles = sorted(archive_dir.glob("root-*.nexus"))
    assert len(bundles) == 1

    _run_cli(
        repo_root,
        target_env,
        "archive",
        "restore",
        str(bundles[0]),
        "--target-zone",
        "root",
        "--force",
    )
    restored = _run_cli(repo_root, target_env, "cat", "/eng/readme.md")
    payload = json.loads(restored.stdout)
    assert payload["data"]["content"] == "known fixture phrase"

#!/usr/bin/env python3
"""Build the public Pages artifact for Nexus and nexus-fs docs.

The repository only has one GitHub Pages site. Root docs are served from `/`,
while `nexus-fs` docs are published as static sub-sites under `/0.1.0/` and
`/latest/`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "site"
NEXUS_FS_DIR = ROOT / "packages" / "nexus-fs"
REPO_SITE_URL = "https://nexi-lab.github.io/nexus"


def run(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def load_nexus_fs_version() -> str:
    with (NEXUS_FS_DIR / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return str(data["project"]["version"])


def build_root_docs() -> None:
    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    run(sys.executable, "-m", "mkdocs", "build", "--site-dir", str(SITE_DIR))


def build_nexus_fs_docs(version: str, alias: str) -> None:
    target_dir = SITE_DIR / alias
    if target_dir.exists():
        shutil.rmtree(target_dir)

    env = os.environ.copy()
    env["NEXUS_FS_SITE_URL"] = f"{REPO_SITE_URL}/{alias}/"
    run(
        sys.executable,
        "-m",
        "mkdocs",
        "build",
        "--strict",
        "--site-dir",
        str(target_dir),
        cwd=NEXUS_FS_DIR,
        env=env,
    )


def main() -> int:
    version = load_nexus_fs_version()
    build_root_docs()
    build_nexus_fs_docs(version, version)
    build_nexus_fs_docs(version, "latest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fixtures: build the slim wheel and install it into a fresh venv.

Both fixtures are session-scoped so the wheel is built once per pytest
run regardless of how many tests use it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SLIM_PKG_DIR = REPO_ROOT / "packages" / "nexus-fs"


@pytest.fixture(scope="session")
def slim_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the slim wheel in a tmpdir and return the .whl path."""
    out_dir = tmp_path_factory.mktemp("slim-wheel")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=SLIM_PKG_DIR,
        check=True,
    )
    wheels = list(out_dir.glob("nexus_fs-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected exactly one nexus_fs wheel in {out_dir}, found {wheels}")
    return wheels[0]


@pytest.fixture(scope="session")
def slim_venv(
    slim_wheel: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Create a fresh venv and pip install the slim wheel into it.

    Returns the venv root.
    """
    venv_dir = tmp_path_factory.mktemp("slim-venv")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    py = _venv_python(venv_dir)
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(py), "-m", "pip", "install", str(slim_wheel)], check=True)
    return venv_dir


def _venv_python(venv_dir: Path) -> Path:
    py = venv_dir / "bin" / "python"
    if not py.exists():
        py = venv_dir / "Scripts" / "python.exe"
    return py


def run_in_slim_venv(venv_dir: Path, code: str) -> subprocess.CompletedProcess[str]:
    """Run a Python snippet inside the slim venv. Captures stdout/stderr."""
    py = _venv_python(venv_dir)
    return subprocess.run(
        [str(py), "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

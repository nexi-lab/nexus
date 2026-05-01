"""Fixtures: build the slim wheel and install it into a fresh venv.

Both fixtures are session-scoped so the wheel is built once per pytest
run regardless of how many tests use it.

Build approach mirrors release-nexus-fs.yml: hatchling rejects ``../../``
paths in isolated build envs, so we create a temporary ``src`` symlink in
``packages/nexus-fs/`` and patch pyproject.toml before building, then
restore both immediately after.

nexus-runtime is not on PyPI.  Set ``NEXUS_RUNTIME_WHEEL_DIR`` to a
directory containing the locally-built ``nexus_runtime-*.whl`` so the
venv install can find it.  In CI this is populated by the build-rust job.
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
    """Build the slim wheel in a tmpdir and return the .whl path.

    Replicates the release-nexus-fs.yml build sequence:
    1. Create ``packages/nexus-fs/src`` symlink → ``../../src``
    2. Patch pyproject.toml to use ``src/nexus`` (hatchling forbids ``../../``)
    3. Run ``hatchling build --target wheel``
    4. Restore pyproject.toml and remove the symlink
    """
    out_dir = tmp_path_factory.mktemp("slim-wheel")
    src_link = SLIM_PKG_DIR / "src"
    pyproject = SLIM_PKG_DIR / "pyproject.toml"
    original_text = pyproject.read_text()
    created_link = False

    try:
        if not src_link.exists():
            src_link.symlink_to((SLIM_PKG_DIR / "../../src").resolve())
            created_link = True
        pyproject.write_text(original_text.replace('"../../src/nexus"', '"src/nexus"'))
        subprocess.run(
            [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(out_dir)],
            cwd=SLIM_PKG_DIR,
            check=True,
        )
    finally:
        pyproject.write_text(original_text)
        if created_link and src_link.is_symlink():
            src_link.unlink()

    wheels = list(out_dir.glob("nexus_fs-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected exactly one nexus_fs wheel in {out_dir}, found {wheels}")
    return wheels[0]


def _install_runtime(py: Path, runtime_wheel_dir: str | None) -> None:
    """Pre-install nexus-runtime from a local wheel dir if provided."""
    if runtime_wheel_dir:
        subprocess.run(
            [
                str(py),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                runtime_wheel_dir,
                "nexus-runtime",
            ],
            check=True,
        )


@pytest.fixture(scope="session")
def slim_base_venv(
    slim_wheel: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Fresh venv with only the base slim wheel (no connector extras).

    Used by test_slim_base_module_imports to verify that modules shipped in
    the base wheel do not secretly depend on extras-only packages.
    """
    venv_dir = tmp_path_factory.mktemp("slim-base-venv")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    py = _venv_python(venv_dir)
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    _install_runtime(py, os.environ.get("NEXUS_RUNTIME_WHEEL_DIR"))
    subprocess.run([str(py), "-m", "pip", "install", str(slim_wheel)], check=True)
    return venv_dir


@pytest.fixture(scope="session")
def slim_venv(
    slim_wheel: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Fresh venv with slim wheel + connector extras (x, gdrive, gmail, gcalendar, slack).

    Used by CRUD and connector-import tests. If ``NEXUS_RUNTIME_WHEEL_DIR`` is
    set the nexus-runtime wheel is pre-installed from that directory (it is not
    on PyPI; CI builds it from source via build-rust-extensions).

    Returns the venv root.
    """
    venv_dir = tmp_path_factory.mktemp("slim-venv")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    py = _venv_python(venv_dir)
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    _install_runtime(py, os.environ.get("NEXUS_RUNTIME_WHEEL_DIR"))
    subprocess.run(
        [str(py), "-m", "pip", "install", f"{slim_wheel}[x,gdrive,gmail,gcalendar,slack]"],
        check=True,
    )
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

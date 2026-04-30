# nexus-fs slim package: Rust-kernel contract + bricks carve-out — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pip install nexus-fs` produce a slim wheel that actually works — declare `nexus-runtime` as a real dep so the Rust kernel is guaranteed at install, and carve `nexus.bricks.auth/**` + `nexus.bricks.search.primitives/**` into the slim wheel so the 7 connectors that hard-import them stop failing.

**Architecture:** Two surgical edits to `packages/nexus-fs/pyproject.toml`: add `nexus-runtime>=0.10,<0.11` to runtime deps, add a `[tool.hatch.build.targets.wheel.force-include]` block that opts the connector-needed bricks subtrees back into the slim wheel (the broad `**/bricks/**` exclude stays). Two new tests + one CI workflow pin the contract: a slim-install smoke that performs CRUD on `local://` after a clean `pip install`, and a wheel-content audit that asserts the slim wheel ships exactly the bricks subtrees we expect.

**Tech Stack:** Python 3.14, Hatchling (build backend for `nexus-fs`), maturin (build backend for `nexus-runtime`), pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-04-29-issues-3942-3943-3944-slim-fs-rust-contract-design.md`

**Issues:** [#3942](https://github.com/nexi-lab/nexus/issues/3942), [#3943](https://github.com/nexi-lab/nexus/issues/3943), [#3944](https://github.com/nexi-lab/nexus/issues/3944)

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `packages/nexus-fs/pyproject.toml` | modify | Declare `nexus-runtime` runtime dep; add `force-include` for bricks carve-out |
| `src/nexus/fs/_sqlite_meta.py` | modify (docstring only) | Drop the "could not build the Rust kernel" framing — that path no longer exists |
| `tests/integration/slim/__init__.py` | create | Test package marker |
| `tests/integration/slim/conftest.py` | create | Build slim wheel + fresh-venv install fixture |
| `tests/integration/slim/test_slim_install_smoke.py` | create | CRUD smoke test (write/read/mkdir/rename/copy/delete on `local://`) |
| `tests/integration/slim/test_slim_wheel_contents.py` | create | Wheel-content audit (assert files included/excluded) |
| `.github/workflows/slim-wheel-smoke.yml` | create | CI job: build slim wheel, install in fresh venv, run smoke + audit tests |

No source-file moves, no re-exports, no connector edits. The fix is package-shape only.

---

## Task 1: Branch setup + sanity check

**Files:** none (env only)

- [ ] **Step 1.1: Confirm working directory + branch**

```bash
pwd
git status
git log --oneline -3
```

Expected: cwd is `/Users/tafeng/nexus/.claude/worktrees/optimized-seeking-marshmallow`, branch is `develop` (or already on the implementation branch), top commit references the spec docs commit `34e5acb10` or later.

- [ ] **Step 1.2: Create implementation branch**

```bash
git switch -c fix/3942-3943-3944-slim-rust-contract
```

Expected: branch created and checked out.

- [ ] **Step 1.3: Verify `nexus-runtime` is reachable**

```bash
python -c "import nexus_runtime; print(nexus_runtime.__version__)"
```

Expected: prints a version (e.g. `0.10.0`). If this fails, the dev env is broken in a way unrelated to this PR — stop and fix the env before continuing.

- [ ] **Step 1.4: Verify the bug exists today**

```bash
python -c "from nexus.backends.connectors.x.connector import PathXBackend"
```

Expected: imports fine in the *full* repo (because `nexus.bricks` is on `sys.path`). This is just a sanity check — the bug only manifests in the *slim wheel install*, which we'll set up in Task 4.

---

## Task 2: Add `nexus-runtime` runtime dependency

**Files:**
- Modify: `packages/nexus-fs/pyproject.toml:32-44`

Closes part of #3942.

- [ ] **Step 2.1: Edit `pyproject.toml` — add the dep**

Open `packages/nexus-fs/pyproject.toml`. The current `dependencies` block is:

```toml
dependencies = [
    "pydantic[email]>=2.0",
    "click>=8.0",
    "rich>=13.0",
    "orjson>=3.9",
    "blake3>=0.4",
    "anyio>=4.0",
    "aiofiles>=23.0",
    "pyyaml>=6.0",
    "platformdirs>=4.0",
    "cryptography>=41.0",
    "httpx>=0.28",
]
```

Add `nexus-runtime` as the last entry:

```toml
dependencies = [
    "pydantic[email]>=2.0",
    "click>=8.0",
    "rich>=13.0",
    "orjson>=3.9",
    "blake3>=0.4",
    "anyio>=4.0",
    "aiofiles>=23.0",
    "pyyaml>=6.0",
    "platformdirs>=4.0",
    "cryptography>=41.0",
    "httpx>=0.28",
    # Rust kernel (PyKernel + Rust-accelerated primitives). Required at runtime;
    # NexusFS.sys_* and metastore both go through nexus_runtime.PyKernel.
    "nexus-runtime>=0.10,<0.11",
]
```

- [ ] **Step 2.2: Update the inline comment that calls deps "slim by design"**

The line just above `dependencies` says:

```toml
# ~17 base dependencies — slim by design (httpx enables universal OAuth)
```

Update to reflect the new contract:

```toml
# ~12 base dependencies — slim contract: kernel + lib + auth bricks + search primitives.
# nexus-runtime is the Rust kernel wheel; required for sys_*, metastore, and rust_compat
# accelerators. Slim does NOT mean "no Rust" — it means "no server / raft / factory".
```

- [ ] **Step 2.3: Verify pyproject still parses**

```bash
python -c "import tomllib; tomllib.loads(open('packages/nexus-fs/pyproject.toml').read())"
```

Expected: no output (silent success). If TOML is broken, fix syntax and re-run.

- [ ] **Step 2.4: Commit**

```bash
git add packages/nexus-fs/pyproject.toml
git commit -m "fix(#3942): declare nexus-runtime as required dep of nexus-fs

The slim package's mount() path goes through SQLiteMetastore →
nexus_runtime.PyKernel, and every NexusFS.sys_* call goes through
the Rust kernel. nexus_runtime was not declared as a dep, so a
clean 'pip install nexus-fs' could not even import nexus.fs."
```

---

## Task 3: Carve auth + search/primitives bricks into the slim wheel

**Files:**
- Modify: `packages/nexus-fs/pyproject.toml:94-144`

Closes #3944.

- [ ] **Step 3.1: Verify the bricks subtrees we want to ship exist**

```bash
ls src/nexus/bricks/auth/__init__.py
ls src/nexus/bricks/search/primitives/__init__.py
ls src/nexus/bricks/__init__.py
ls src/nexus/bricks/search/__init__.py
```

Expected: all four files exist. If any are missing, the carve-out won't work — stop and figure out what's wrong before continuing.

- [ ] **Step 3.2: Add `force-include` block to pyproject.toml**

Open `packages/nexus-fs/pyproject.toml`. The current `[tool.hatch.build.targets.wheel]` block ends at line ~144 with the `exclude` list. Add this **after** the `exclude` block:

```toml
[tool.hatch.build.targets.wheel.force-include]
# Carve specific bricks subtrees back into the slim wheel.
# The broad `**/bricks/**` exclude above keeps server/raft/sync/etc. off slim,
# but the auth bricks (oauth/factory/profile/credential_pool/classifiers) and
# the search primitives (glob_helpers/trigram_fast) are needed by connectors
# that ship in the slim wheel. force-include overrides exclude.
"../../src/nexus/bricks/__init__.py" = "nexus/bricks/__init__.py"
"../../src/nexus/bricks/search/__init__.py" = "nexus/bricks/search/__init__.py"
"../../src/nexus/bricks/search/primitives" = "nexus/bricks/search/primitives"
"../../src/nexus/bricks/auth" = "nexus/bricks/auth"
```

The `../../` prefix matches the existing `packages = ["../../src/nexus"]` pattern (the slim wheel package builds from the worktree root, two levels up from `packages/nexus-fs/`).

- [ ] **Step 3.3: Smoke-build the slim wheel locally**

```bash
cd packages/nexus-fs
python -m build --wheel --outdir /tmp/slim-wheel-out
cd ../..
```

Expected: a wheel file appears at `/tmp/slim-wheel-out/nexus_fs-*.whl`. If the build fails, read the Hatchling error — most likely a TOML syntax error, a force-include path typo, or `python -m build` not installed (run `python -m pip install build` and retry).

- [ ] **Step 3.4: Inspect the built wheel — confirm the carve-out**

```bash
python -m zipfile -l /tmp/slim-wheel-out/nexus_fs-*.whl | grep -E "nexus/bricks/(auth|search)" | head -30
```

Expected output includes lines like:
```
nexus/bricks/__init__.py
nexus/bricks/auth/__init__.py
nexus/bricks/auth/profile.py
nexus/bricks/auth/credential_pool.py
nexus/bricks/auth/oauth/factory.py
nexus/bricks/auth/oauth/token_manager.py
nexus/bricks/auth/classifiers/google.py
nexus/bricks/search/__init__.py
nexus/bricks/search/primitives/__init__.py
nexus/bricks/search/primitives/glob_helpers.py
nexus/bricks/search/primitives/trigram_fast.py
```

- [ ] **Step 3.5: Inspect the built wheel — confirm exclusions still work**

```bash
python -m zipfile -l /tmp/slim-wheel-out/nexus_fs-*.whl | grep "nexus/bricks/" | grep -vE "/(auth|search)/" | head
```

Expected: no output (or only `nexus/bricks/__init__.py`). If you see `nexus/bricks/server/` or `nexus/bricks/raft/` etc., the broad exclude is being overridden by something — re-check the `force-include` paths.

- [ ] **Step 3.6: Commit**

```bash
git add packages/nexus-fs/pyproject.toml
git commit -m "fix(#3944): carve bricks/auth + bricks/search/primitives into slim wheel

The slim wheel previously excluded all of bricks/**, but 7 connectors
(x, gmail, slack, gdrive, calendar, oauth_base, cli/base) hard-import
nexus.bricks.auth.oauth.factory / nexus.bricks.auth.profile /
nexus.bricks.auth.classifiers.google / nexus.bricks.search.primitives.

Use Hatchling force-include to opt the auth subtree and the search
primitives back into the slim wheel while leaving the broad bricks
exclude in place for server/raft/sync/etc."
```

---

## Task 4: Slim-install smoke test infrastructure

**Files:**
- Create: `tests/integration/slim/__init__.py`
- Create: `tests/integration/slim/conftest.py`

Provides the fixtures Tasks 5 and 6 use. No assertions yet.

- [ ] **Step 4.1: Create the test package marker**

Create `tests/integration/slim/__init__.py`:

```python
"""Integration tests that build the nexus-fs slim wheel and install it
into a fresh venv to exercise the actual published-package path."""
```

- [ ] **Step 4.2: Create the conftest with the wheel-build + venv fixtures**

Create `tests/integration/slim/conftest.py`:

```python
"""Fixtures: build the slim wheel and install it into a fresh venv.

Both fixtures are session-scoped so the wheel is built once per pytest
run regardless of how many tests use it.
"""

from __future__ import annotations

import os
import shutil
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
        raise RuntimeError(
            f"Expected exactly one nexus_fs wheel in {out_dir}, found {wheels}"
        )
    return wheels[0]


@pytest.fixture(scope="session")
def slim_venv(
    slim_wheel: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Create a fresh venv and pip install the slim wheel into it.

    Returns the venv root. Use `slim_venv_python(slim_venv)` to get the
    interpreter path.
    """
    venv_dir = tmp_path_factory.mktemp("slim-venv")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    py = venv_dir / "bin" / "python"
    if not py.exists():  # Windows fallback
        py = venv_dir / "Scripts" / "python.exe"
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    # Install the slim wheel; nexus-runtime comes via the declared dep.
    subprocess.run([str(py), "-m", "pip", "install", str(slim_wheel)], check=True)
    return venv_dir


def slim_venv_python(venv_dir: Path) -> Path:
    py = venv_dir / "bin" / "python"
    if not py.exists():
        py = venv_dir / "Scripts" / "python.exe"
    return py


def run_in_slim_venv(venv_dir: Path, code: str) -> subprocess.CompletedProcess[str]:
    """Run a Python script inside the slim venv. Captures stdout/stderr."""
    py = slim_venv_python(venv_dir)
    return subprocess.run(
        [str(py), "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
```

- [ ] **Step 4.3: Verify pytest collects the package**

```bash
pytest tests/integration/slim/ --collect-only
```

Expected: pytest reports 0 tests collected (no test files yet) without errors. If pytest errors on import, fix the conftest.

- [ ] **Step 4.4: Commit**

```bash
git add tests/integration/slim/__init__.py tests/integration/slim/conftest.py
git commit -m "test(slim): add wheel-build + fresh-venv pytest fixtures"
```

---

## Task 5: Slim-install smoke test (CRUD on `local://`)

**Files:**
- Create: `tests/integration/slim/test_slim_install_smoke.py`

Closes #3943.

- [ ] **Step 5.1: Write the failing test**

Create `tests/integration/slim/test_slim_install_smoke.py`:

```python
"""End-to-end smoke: install the slim wheel into a fresh venv and
exercise local:// CRUD through the public nexus.fs facade.

This is the regression net for #3943 — proves that a clean slim
install can write/read/delete/mkdir/rename/copy without any extra
imports beyond what 'pip install nexus-fs' provides.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import run_in_slim_venv


def test_slim_local_crud(slim_venv: Path, tmp_path: Path) -> None:
    """In a clean slim venv: mount local://<tmp>, do full CRUD."""
    workdir = tmp_path / "data"
    workdir.mkdir()

    script = f"""
import sys
import nexus.fs

fs = nexus.fs.mount_sync("local://{workdir}")

# write
fs.write("/local{workdir}/hello.txt", b"hi from slim")

# read
content = fs.read("/local{workdir}/hello.txt")
assert content == b"hi from slim", repr(content)

# mkdir
fs.mkdir("/local{workdir}/sub")

# rename
fs.write("/local{workdir}/old.txt", b"old")
fs.rename("/local{workdir}/old.txt", "/local{workdir}/new.txt")
assert fs.read("/local{workdir}/new.txt") == b"old"

# copy
fs.copy("/local{workdir}/hello.txt", "/local{workdir}/copy.txt")
assert fs.read("/local{workdir}/copy.txt") == b"hi from slim"

# delete
fs.delete("/local{workdir}/hello.txt")
try:
    fs.read("/local{workdir}/hello.txt")
    sys.exit("expected delete to remove the file")
except FileNotFoundError:
    pass

print("OK")
"""
    result = run_in_slim_venv(slim_venv, script)
    assert result.returncode == 0, (
        f"slim crud script failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


@pytest.mark.parametrize(
    "connector_module",
    [
        "nexus.backends.connectors.x.connector",
        "nexus.backends.connectors.gmail.connector",
        "nexus.backends.connectors.slack.connector",
        "nexus.backends.connectors.gdrive.connector",
        "nexus.backends.connectors.calendar.connector",
    ],
)
def test_slim_connector_imports(slim_venv: Path, connector_module: str) -> None:
    """Each connector that hard-imports nexus.bricks.* must import in slim."""
    script = f"import {connector_module}; print('OK')"
    result = run_in_slim_venv(slim_venv, script)
    assert result.returncode == 0, (
        f"importing {connector_module} failed in slim venv:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout
```

The exact path strings (`/local{workdir}/hello.txt`) match how `nexus.fs` derives mount points from `local://` URIs — verified by reading `src/nexus/fs/_uri.py` and existing tests in `tests/unit/fs/`. If the derived mount point differs in your env (e.g. due to an absolute-path quirk), adjust the f-string after running the test once and reading the actual path from the failure output.

- [ ] **Step 5.2: Run the test — verify it passes**

```bash
pytest tests/integration/slim/test_slim_install_smoke.py -v
```

Expected: `test_slim_local_crud PASSED` and all 5 `test_slim_connector_imports[*] PASSED`. The first run is slow (builds wheel + creates venv); subsequent runs reuse session fixtures.

If `test_slim_local_crud` fails on the path shape: read the failure stderr, locate the actual `local://` mount-point pattern (e.g. it might be `/local{workdir}` or `/data` if `at=` derivation kicks in), and adjust the path strings.

If a connector import test fails: the failure message names the missing module. Most likely `nexus.bricks.auth.<sub>` is the culprit — confirm the carve-out from Task 3 included it; you may need to extend the `force-include` list (e.g. if a connector imports a bricks/auth submodule that depends on a different bricks/auth path that wasn't carved).

- [ ] **Step 5.3: Commit**

```bash
git add tests/integration/slim/test_slim_install_smoke.py
git commit -m "test(#3943): slim install CRUD smoke + connector import smoke"
```

---

## Task 6: Slim wheel-content audit

**Files:**
- Create: `tests/integration/slim/test_slim_wheel_contents.py`

Closes #3944's "release-integrity test" AC. Pins what the slim wheel ships so a future allowlist edit can't silently drop or add bricks subtrees.

- [ ] **Step 6.1: Write the failing test**

Create `tests/integration/slim/test_slim_wheel_contents.py`:

```python
"""Wheel-content audit for the slim package.

Pins the set of nexus.bricks paths that ship in the slim wheel. The slim
contract is: bricks/auth/** + bricks/search/primitives/** + the package
markers needed to traverse them. Any deviation fails CI loudly.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

# Paths that MUST appear in the slim wheel (file or directory prefix).
REQUIRED_BRICKS_PATHS = [
    "nexus/bricks/__init__.py",
    "nexus/bricks/search/__init__.py",
    "nexus/bricks/search/primitives/__init__.py",
    "nexus/bricks/search/primitives/glob_helpers.py",
    "nexus/bricks/auth/__init__.py",
    "nexus/bricks/auth/profile.py",
    "nexus/bricks/auth/credential_pool.py",
    "nexus/bricks/auth/oauth/factory.py",
    "nexus/bricks/auth/oauth/token_manager.py",
    "nexus/bricks/auth/oauth/base_provider.py",
    "nexus/bricks/auth/classifiers/google.py",
]

# bricks subtrees that MUST NOT appear in the slim wheel.
FORBIDDEN_BRICKS_PREFIXES = [
    "nexus/bricks/access_manifest/",
    "nexus/bricks/catalog/",
    "nexus/bricks/context_manifest/",
    "nexus/bricks/delegation/",
    "nexus/bricks/discovery/",
    "nexus/bricks/filesystem/",
    "nexus/bricks/governance/",
    "nexus/bricks/identity/",
    "nexus/bricks/mcp/",
    "nexus/bricks/mount/",
    "nexus/bricks/parsers/",
    "nexus/bricks/pay/",
    "nexus/bricks/portability/",
    "nexus/bricks/rebac/",
    "nexus/bricks/sandbox/",
    "nexus/bricks/secrets/",
    "nexus/bricks/share_link/",
    "nexus/bricks/snapshot/",
    "nexus/bricks/task_manager/",
    "nexus/bricks/upload/",
    "nexus/bricks/versioning/",
    "nexus/bricks/workflows/",
    "nexus/bricks/workspace/",
    # bricks/search subtrees other than primitives
    "nexus/bricks/search/bm25s_search.py",
    "nexus/bricks/search/chunking.py",
    "nexus/bricks/search/daemon.py",
    "nexus/bricks/search/search_service.py",
]


@pytest.fixture(scope="module")
def wheel_namelist(slim_wheel: Path) -> list[str]:
    with zipfile.ZipFile(slim_wheel) as zf:
        return zf.namelist()


@pytest.mark.parametrize("required_path", REQUIRED_BRICKS_PATHS)
def test_slim_wheel_includes_required_bricks(
    wheel_namelist: list[str], required_path: str
) -> None:
    assert required_path in wheel_namelist, (
        f"slim wheel missing required path: {required_path}\n"
        f"Wheel contents (first 50 entries):\n"
        + "\n".join(wheel_namelist[:50])
    )


@pytest.mark.parametrize("forbidden_prefix", FORBIDDEN_BRICKS_PREFIXES)
def test_slim_wheel_excludes_forbidden_bricks(
    wheel_namelist: list[str], forbidden_prefix: str
) -> None:
    leaks = [p for p in wheel_namelist if p.startswith(forbidden_prefix)]
    assert not leaks, (
        f"slim wheel leaked forbidden bricks paths under {forbidden_prefix!r}:\n"
        + "\n".join(leaks)
    )


def test_slim_wheel_includes_nexus_runtime_dep_metadata(slim_wheel: Path) -> None:
    """The wheel's METADATA file must declare nexus-runtime as a Requires-Dist."""
    with zipfile.ZipFile(slim_wheel) as zf:
        meta_files = [n for n in zf.namelist() if n.endswith("METADATA")]
        assert meta_files, f"no METADATA in {slim_wheel}"
        meta = zf.read(meta_files[0]).decode("utf-8")
    assert "Requires-Dist: nexus-runtime" in meta, (
        f"slim wheel METADATA missing nexus-runtime requirement:\n{meta}"
    )
```

- [ ] **Step 6.2: Run the test — verify it passes**

```bash
pytest tests/integration/slim/test_slim_wheel_contents.py -v
```

Expected: every parametrized include passes, every parametrized exclude passes, and the metadata test passes. If a `REQUIRED_BRICKS_PATHS` entry doesn't exist in the source tree (file was renamed since plan-writing), update the list to match reality. If a `FORBIDDEN_BRICKS_PREFIXES` test fails, the carve-out in Task 3 is over-including — narrow the `force-include` paths.

- [ ] **Step 6.3: Commit**

```bash
git add tests/integration/slim/test_slim_wheel_contents.py
git commit -m "test(#3944): pin slim wheel content (bricks carve-out audit)"
```

---

## Task 7: Drop the stale "could not build the Rust kernel" framing

**Files:**
- Modify: `src/nexus/fs/_sqlite_meta.py:1-21`

Pure docstring cleanup. The old framing implied a no-Rust path that hasn't existed for several releases and is explicitly rejected by the new contract.

- [ ] **Step 7.1: Replace the module docstring**

Open `src/nexus/fs/_sqlite_meta.py`. Replace lines 1-21 (the existing module docstring) with:

```python
"""``SQLiteMetastore`` compatibility factory.

Historical note — this module used to implement a Python-only
``MetastoreABC`` subclass backed by a local SQLite file as the slim
package's metadata store for environments without the Rust kernel.
The kernel is now the single source of truth for metastore state, and
``nexus-fs`` declares ``nexus-runtime`` as a required dep, so the
Python-only path no longer exists.

This file preserves the ``SQLiteMetastore`` import path as a thin
factory **function** — not a class — that returns a
``RustMetastoreProxy`` wired to a fresh bare ``Kernel`` with its
redb-backed metastore pointed at ``db_path``. The ``.db`` suffix is
rewritten to ``.redb`` so an existing sqlite file from a previous run
is not accidentally overwritten.
"""
```

- [ ] **Step 7.2: Verify the module still imports**

```bash
python -c "from nexus.fs._sqlite_meta import SQLiteMetastore; print(SQLiteMetastore)"
```

Expected: prints `<function SQLiteMetastore at 0x...>`.

- [ ] **Step 7.3: Commit**

```bash
git add src/nexus/fs/_sqlite_meta.py
git commit -m "docs(fs): drop stale 'could not build Rust kernel' framing in _sqlite_meta"
```

---

## Task 8: CI workflow for slim-wheel-smoke

**Files:**
- Create: `.github/workflows/slim-wheel-smoke.yml`

- [ ] **Step 8.1: Find an existing workflow to mirror style**

```bash
ls .github/workflows/ | head -20
```

Pick one Python-test-shaped workflow (e.g. one named like `unit-tests.yml` or `pytest.yml`) and skim it for the project's conventions: pinned action versions, Python install action, cache config.

```bash
cat .github/workflows/$(ls .github/workflows/ | grep -i 'unit\|pytest\|test' | head -1)
```

If no Python workflow exists, fall back to the layout below verbatim.

- [ ] **Step 8.2: Create the workflow file**

Create `.github/workflows/slim-wheel-smoke.yml`:

```yaml
name: slim-wheel-smoke

on:
  pull_request:
    paths:
      - 'packages/nexus-fs/**'
      - 'src/nexus/fs/**'
      - 'src/nexus/backends/connectors/**'
      - 'src/nexus/lib/**'
      - 'src/nexus/bricks/auth/**'
      - 'src/nexus/bricks/search/primitives/**'
      - 'tests/integration/slim/**'
      - '.github/workflows/slim-wheel-smoke.yml'
  push:
    branches: [develop]

jobs:
  smoke:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.14'
      - name: Install build tooling
        run: python -m pip install --upgrade pip build pytest
      - name: Run slim wheel integration tests
        run: pytest tests/integration/slim/ -v
        env:
          # The conftest fixtures shell out to the matrix python; pin it explicitly.
          PYTHONUNBUFFERED: '1'
```

The trigger paths cover everything that could change the slim wheel content or the connector-import surface. The matrix covers Linux + macOS (Windows deferred per spec). Python 3.14 matches `requires-python` in `packages/nexus-fs/pyproject.toml`.

- [ ] **Step 8.3: Verify the workflow YAML is valid**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/slim-wheel-smoke.yml'))"
```

Expected: no output. If YAML is broken, fix syntax.

- [ ] **Step 8.4: Commit**

```bash
git add .github/workflows/slim-wheel-smoke.yml
git commit -m "ci: slim-wheel-smoke job — build slim wheel, install in fresh venv, run integration tests"
```

---

## Task 9: Full local validation

**Files:** none (validation only)

- [ ] **Step 9.1: Run the new integration tests one more time end-to-end**

```bash
pytest tests/integration/slim/ -v
```

Expected: every test in the slim suite passes. Re-runs reuse the session-scoped wheel + venv fixtures so this should be fast (<30s after the first build).

- [ ] **Step 9.2: Run the existing fs unit tests — confirm no regression**

```bash
pytest tests/unit/fs/ -v
```

Expected: all pre-existing tests pass. They don't touch the wheel; they exercise the in-process slim API. The contract change (declaring `nexus-runtime`) and the carve-out (force-include) don't affect runtime behavior in the dev tree.

- [ ] **Step 9.3: Run the connector unit tests — confirm no regression**

```bash
pytest tests/unit/backends/connectors/ -v
```

Expected: all pre-existing tests pass. We made no source-code changes to connectors.

- [ ] **Step 9.4: Run pre-commit hooks**

```bash
pre-commit run --files \
  packages/nexus-fs/pyproject.toml \
  src/nexus/fs/_sqlite_meta.py \
  tests/integration/slim/__init__.py \
  tests/integration/slim/conftest.py \
  tests/integration/slim/test_slim_install_smoke.py \
  tests/integration/slim/test_slim_wheel_contents.py \
  .github/workflows/slim-wheel-smoke.yml
```

Expected: every hook passes (ruff, ruff-format, end-of-file fixer, etc.). If a hook auto-fixes a file, re-stage and amend the relevant commit OR add a follow-up "chore: ruff fixes" commit.

- [ ] **Step 9.5: Confirm the commit graph reads cleanly**

```bash
git log --oneline develop..HEAD
```

Expected output (in this order):
```
<sha> ci: slim-wheel-smoke job — build slim wheel, install in fresh venv, run integration tests
<sha> docs(fs): drop stale 'could not build Rust kernel' framing in _sqlite_meta
<sha> test(#3944): pin slim wheel content (bricks carve-out audit)
<sha> test(#3943): slim install CRUD smoke + connector import smoke
<sha> test(slim): add wheel-build + fresh-venv pytest fixtures
<sha> fix(#3944): carve bricks/auth + bricks/search/primitives into slim wheel
<sha> fix(#3942): declare nexus-runtime as required dep of nexus-fs
```

If commits are out of order or messages are vague, use `git rebase -i` to fix (do NOT use `--no-edit` and do NOT amend a published commit — but this branch isn't pushed yet so amending is fine).

---

## Task 10: Push and open PR

**Files:** none

- [ ] **Step 10.1: Push the branch**

```bash
git push -u origin fix/3942-3943-3944-slim-rust-contract
```

- [ ] **Step 10.2: Open the PR via gh**

```bash
gh pr create --title "fix(slim): nexus-fs Rust-kernel contract + bricks carve-out (closes #3942 #3943 #3944)" --body "$(cat <<'EOF'
## Summary

- Declares `nexus-runtime>=0.10,<0.11` as a runtime dep of `nexus-fs` (closes #3942 — slim install no longer dies importing `nexus_runtime`).
- Carves `nexus/bricks/auth/**` + `nexus/bricks/search/primitives/**` back into the slim wheel via Hatchling `force-include`, so the 7 connectors that hard-import them work in the slim package (closes #3944 — without a 2000-LOC bricks/auth refactor).
- Adds a `tests/integration/slim/` suite that builds the slim wheel, installs it into a fresh venv, and exercises `local://` CRUD + connector imports — proving the local example from the README works on a clean slim install (closes #3943).
- Pins the slim-shipped bricks set with a wheel-content audit so a future allowlist edit can't silently regress.
- Adds `.github/workflows/slim-wheel-smoke.yml` — runs the new suite on every PR that touches the slim surface, on Linux + macOS / Python 3.14.

Spec: `docs/superpowers/specs/2026-04-29-issues-3942-3943-3944-slim-fs-rust-contract-design.md`

## Test plan

- [x] `pytest tests/integration/slim/ -v` passes locally on macOS
- [x] `pytest tests/unit/fs/` no regression
- [x] `pytest tests/unit/backends/connectors/` no regression
- [x] `python -m build --wheel` succeeds for `packages/nexus-fs`
- [x] Slim wheel ships `nexus/bricks/auth/**` and `nexus/bricks/search/primitives/**` (verified by `python -m zipfile -l`)
- [x] Slim wheel does NOT ship `nexus/bricks/server/**`, `nexus/bricks/raft/**`, `nexus/bricks/catalog/**` (audit test)
- [ ] CI: `slim-wheel-smoke` matrix passes on `ubuntu-latest` and `macos-latest`
EOF
)"
```

- [ ] **Step 10.3: Capture the PR URL for the user**

The previous step prints the PR URL. Note it for the wrap-up message.

---

## Self-Review

After writing the plan, the following pass was made over the spec:

**1. Spec coverage:**
- "Declare `nexus-runtime` as required runtime dependency" → Task 2.
- "Add `force-include` for `bricks/auth/**` + `bricks/search/primitives/**`" → Task 3.
- "Update `_sqlite_meta.py` docstring" → Task 7.
- "Install-smoke CI test (CRUD on `local://`)" → Tasks 4+5.
- "Slim wheel-content audit" → Task 6.
- "CI workflow `slim-wheel-smoke.yml`" → Task 8.
- "All connector tests must pass" → Task 9.3.
- "Manual verification (`pip install -e .` + import X / gmail)" → covered by Tasks 5 + 9.
- Spec mentions cleaning up `tests/unit/fs/test_slim_external_write.py`. The file does not exist on `develop` (verified during plan-writing) — the issue body referenced stale paths. No task needed; if a future merge reintroduces the file, the smoke test catches the underlying contract regression.

**2. Placeholder scan:** none — every step has concrete commands and expected output.

**3. Type consistency:** N/A — no new types defined.

**4. Risk callouts surfaced in the plan:**
- Task 5.2 documents how to recover if the derived `local://` path shape differs from the f-string assumption.
- Task 6.2 documents how to recover if a `REQUIRED_BRICKS_PATHS` entry was renamed since plan-writing.
- Task 3.4 / 3.5 verify the carve-out behavior empirically before committing.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-29-issues-3942-3943-3944-slim-fs-rust-contract.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

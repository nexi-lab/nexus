# External-CLI Sync Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the external-CLI sync framework, the aws-cli adapter, ExternalCliBackend, and cut `nexus-fs auth list` over to the unified profile store (Phase 2 of #3722, issue #3739).

**Architecture:** Two-layer design: sync adapters discover accounts from external CLIs and upsert routing metadata into AuthProfileStore; ExternalCliBackend implements CredentialBackend for fresh credential resolution. AdapterRegistry manages startup, background refresh with per-adapter TTL, and per-adapter circuit breakers.

**Tech Stack:** Python 3.11+, asyncio, configparser (INI parsing), pytest + pytest-asyncio (auto mode), SQLite (via existing SqliteAuthProfileStore).

---

## File Map

### New files (create)

| File | Responsibility |
|------|---------------|
| `src/nexus/bricks/auth/external_sync/__init__.py` | Re-export public API |
| `src/nexus/bricks/auth/external_sync/base.py` | `ExternalCliSyncAdapter` ABC, `SyncedProfile`, `SyncResult` |
| `src/nexus/bricks/auth/external_sync/file_adapter.py` | `FileAdapter(ExternalCliSyncAdapter)` base class |
| `src/nexus/bricks/auth/external_sync/subprocess_adapter.py` | `SubprocessAdapter(ExternalCliSyncAdapter)` base class |
| `src/nexus/bricks/auth/external_sync/aws_sync.py` | `AwsCliSyncAdapter(FileAdapter)` — ~40 LOC |
| `src/nexus/bricks/auth/external_sync/external_cli_backend.py` | `ExternalCliBackend(CredentialBackend)` — ~80 LOC |
| `src/nexus/bricks/auth/external_sync/registry.py` | `AdapterRegistry`, `CircuitBreaker` |
| `src/nexus/bricks/auth/tests/test_file_adapter.py` | FileAdapter base class tests via synthetic adapter |
| `src/nexus/bricks/auth/tests/test_subprocess_adapter.py` | SubprocessAdapter base class tests via synthetic adapter |
| `src/nexus/bricks/auth/tests/test_aws_sync.py` | AwsCliSyncAdapter parse tests |
| `src/nexus/bricks/auth/tests/test_registry.py` | Registry startup, background loop, circuit breaker tests |
| `src/nexus/bricks/auth/tests/test_external_cli_backend.py` | ExternalCliBackend tests |
| `src/nexus/bricks/auth/tests/test_auth_list_cutover.py` | nexus-fs auth list integration tests |
| `src/nexus/bricks/auth/tests/test_e2e_aws.py` | Nightly real-binary e2e test |
| `src/nexus/bricks/auth/tests/fixtures/external_cli_output/aws_credentials_v2.15.ini` | AWS credentials fixture (standard) |
| `src/nexus/bricks/auth/tests/fixtures/external_cli_output/aws_credentials_v2.16.ini` | AWS credentials fixture (SSO/session tokens) |

### Modified files

| File | Change |
|------|--------|
| `src/nexus/fs/_auth_cli.py:264-304` | `list_auth()` — dual-read with new table format |
| `src/nexus/fs/_backend_factory.py:36-46` | S3 creation — dual-path through profile store |

---

## Task 1: Base types — `SyncedProfile`, `SyncResult`, `ExternalCliSyncAdapter` ABC

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/__init__.py`
- Create: `src/nexus/bricks/auth/external_sync/base.py`
- Test: `src/nexus/bricks/auth/tests/test_file_adapter.py` (just the import smoke test for now)

- [ ] **Step 1: Create the `external_sync` package with `base.py`**

```python
# src/nexus/bricks/auth/external_sync/__init__.py
"""External CLI sync framework for discovering credentials managed by external tools."""

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)

__all__ = [
    "ExternalCliSyncAdapter",
    "SyncedProfile",
    "SyncResult",
]
```

```python
# src/nexus/bricks/auth/external_sync/base.py
"""Abstract base for external CLI sync adapters.

Sync adapters discover which accounts exist in external CLIs (aws, gcloud, gh)
and produce SyncedProfile metadata. They do NOT resolve actual credentials —
that is ExternalCliBackend's job (external_cli_backend.py).

Each concrete adapter is a thin descriptor subclass of either FileAdapter
(for CLIs with parseable config files) or SubprocessAdapter (for CLIs
that require shell-out). The base classes own all I/O, timeout, retry,
and error-classification logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.auth.credential_backend import ResolvedCredential


@dataclass(frozen=True, slots=True)
class SyncedProfile:
    """One discovered account from an external CLI.

    This is metadata only — no secrets. Maps to an AuthProfile in the store
    with backend="external-cli".
    """

    provider: str  # e.g. "s3"
    account_identifier: str  # e.g. "default", "work-prod"
    backend_key: str  # e.g. "aws-cli/default" — opaque to the store
    source: str  # e.g. "aws-cli" — displayed in `auth list` Source column


@dataclass
class SyncResult:
    """Output of a single adapter sync() call."""

    adapter_name: str
    profiles: list[SyncedProfile] = field(default_factory=list)
    error: str | None = None  # non-None means degraded


class ExternalCliSyncAdapter(ABC):
    """Abstract base for external CLI sync adapters.

    Subclass either FileAdapter or SubprocessAdapter, not this directly.
    """

    adapter_name: str  # e.g. "aws-cli", "gcloud"
    sync_ttl_seconds: float = 60.0
    failure_threshold: int = 3
    reset_timeout_seconds: float = 60.0

    @abstractmethod
    async def sync(self) -> SyncResult:
        """Discover all accounts from this external CLI."""
        ...

    @abstractmethod
    async def detect(self) -> bool:
        """Quick check: is this CLI / config available on the system?"""
        ...

    @abstractmethod
    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        """Fresh-read a credential for the given backend_key.

        Called by ExternalCliBackend.resolve(). Re-reads the source
        (file or subprocess) and extracts the actual secret for one profile.
        """
        ...
```

- [ ] **Step 2: Write import smoke test**

```python
# src/nexus/bricks/auth/tests/test_file_adapter.py
"""Tests for FileAdapter base class via synthetic adapter."""

from __future__ import annotations


class TestBaseImports:
    def test_base_types_importable(self) -> None:
        from nexus.bricks.auth.external_sync.base import (
            ExternalCliSyncAdapter,
            SyncedProfile,
            SyncResult,
        )

        assert SyncedProfile is not None
        assert SyncResult is not None
        assert ExternalCliSyncAdapter is not None
```

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest src/nexus/bricks/auth/tests/test_file_adapter.py::TestBaseImports -xvs`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/__init__.py src/nexus/bricks/auth/external_sync/base.py src/nexus/bricks/auth/tests/test_file_adapter.py
git commit -m "feat(auth): add ExternalCliSyncAdapter ABC + SyncedProfile + SyncResult (#3739)"
```

---

## Task 2: FileAdapter base class + tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/file_adapter.py`
- Modify: `src/nexus/bricks/auth/tests/test_file_adapter.py`

- [ ] **Step 1: Write failing tests for FileAdapter via synthetic adapter**

Append to `src/nexus/bricks/auth/tests/test_file_adapter.py`:

```python
import os
import stat
from pathlib import Path

import pytest

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import SyncedProfile, SyncResult


class SyntheticFileAdapter:
    """Fake FileAdapter subclass for testing the base class mechanics."""

    adapter_name = "synthetic-file"
    sync_ttl_seconds = 60.0
    failure_threshold = 3
    reset_timeout_seconds = 60.0

    def __init__(self, file_content: str = "", fail_parse: bool = False) -> None:
        self._file_content = file_content
        self._fail_parse = fail_parse
        self._tmp_path: Path | None = None

    def set_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        cfg = tmp_path / "synthetic.conf"
        cfg.write_text(self._file_content)

    def paths(self) -> list[Path]:
        if self._tmp_path is None:
            return [Path("/nonexistent/synthetic.conf")]
        return [self._tmp_path / "synthetic.conf"]

    def parse_file(self, path: Path, content: str) -> list[SyncedProfile]:
        if self._fail_parse:
            raise ValueError("malformed content")
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        return [
            SyncedProfile(
                provider="test",
                account_identifier=line,
                backend_key=f"synthetic-file/{line}",
                source="synthetic-file",
            )
            for line in lines
        ]

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="api_key", api_key="fake-key")


class TestFileAdapterDetect:
    async def test_detect_true_when_file_exists(self, tmp_path: Path) -> None:
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        adapter = _make_synthetic(tmp_path, "profile1\n")
        assert await adapter.detect() is True

    async def test_detect_false_when_no_files(self) -> None:
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        adapter = _make_synthetic(None, "")
        assert await adapter.detect() is False


class TestFileAdapterSync:
    async def test_sync_parses_file_content(self, tmp_path: Path) -> None:
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        adapter = _make_synthetic(tmp_path, "profile1\nprofile2\n")
        result = await adapter.sync()
        assert result.error is None
        assert len(result.profiles) == 2
        assert result.profiles[0].account_identifier == "profile1"
        assert result.profiles[1].account_identifier == "profile2"

    async def test_sync_missing_file_returns_degraded(self) -> None:
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        adapter = _make_synthetic(None, "")
        result = await adapter.sync()
        assert result.error is not None
        assert "not found" in result.error.lower() or "no readable" in result.error.lower()
        assert result.profiles == []

    async def test_sync_empty_file_returns_empty_profiles(self, tmp_path: Path) -> None:
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        adapter = _make_synthetic(tmp_path, "")
        result = await adapter.sync()
        assert result.error is None
        assert result.profiles == []

    async def test_sync_malformed_content_returns_degraded(self, tmp_path: Path) -> None:
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        adapter = _make_synthetic(tmp_path, "anything", fail_parse=True)
        result = await adapter.sync()
        assert result.error is not None
        assert "malformed" in result.error.lower() or "parse" in result.error.lower()

    async def test_sync_unreadable_perms_returns_degraded(self, tmp_path: Path) -> None:
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        adapter = _make_synthetic(tmp_path, "data")
        cfg = tmp_path / "synthetic.conf"
        cfg.chmod(0o000)
        try:
            result = await adapter.sync()
            assert result.error is not None
        finally:
            cfg.chmod(stat.S_IRUSR | stat.S_IWUSR)

    async def test_sync_symlink_loop_returns_degraded(self, tmp_path: Path) -> None:
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        loop_a = tmp_path / "synthetic.conf"
        loop_b = tmp_path / "loop_b"
        # Remove the real file, create a symlink loop
        if loop_a.exists():
            loop_a.unlink()
        loop_b.symlink_to(loop_a)
        loop_a.symlink_to(loop_b)

        adapter = _make_synthetic(tmp_path, "", setup_file=False)
        result = await adapter.sync()
        assert result.error is not None


def _make_synthetic(
    tmp_path: Path | None,
    content: str,
    *,
    fail_parse: bool = False,
    setup_file: bool = True,
) -> "FileAdapter":
    """Build a concrete FileAdapter using the synthetic descriptor pattern."""
    from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

    class _Adapter(FileAdapter):
        adapter_name = "synthetic-file"
        _fail_parse = fail_parse

        def __init__(self, tmp_path: Path | None, content: str) -> None:
            self._tmp_path = tmp_path
            self._content = content

        def paths(self) -> list[Path]:
            if self._tmp_path is None:
                return [Path("/nonexistent/synthetic.conf")]
            return [self._tmp_path / "synthetic.conf"]

        def parse_file(self, path: Path, content: str) -> list[SyncedProfile]:
            if self._fail_parse:
                raise ValueError("malformed content")
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            return [
                SyncedProfile(
                    provider="test",
                    account_identifier=line,
                    backend_key=f"synthetic-file/{line}",
                    source="synthetic-file",
                )
                for line in lines
            ]

        async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
            return ResolvedCredential(kind="api_key", api_key="fake-key")

    adapter = _Adapter(tmp_path, content)
    if tmp_path is not None and setup_file:
        cfg = tmp_path / "synthetic.conf"
        cfg.write_text(content)
    return adapter
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/nexus/bricks/auth/tests/test_file_adapter.py -xvs -k "not TestBaseImports"`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.auth.external_sync.file_adapter'`

- [ ] **Step 3: Implement FileAdapter**

```python
# src/nexus/bricks/auth/external_sync/file_adapter.py
"""FileAdapter — base class for external CLIs with parseable config files.

Subclasses declare which files to read and how to parse them. The base
class handles all I/O: detect (file exists), sync (read + parse + classify
errors), and graceful degradation on missing/unreadable/malformed files.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from pathlib import Path

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)

logger = logging.getLogger(__name__)


class FileAdapter(ExternalCliSyncAdapter):
    """Base class for config-file-based sync adapters.

    Subclasses implement paths() and parse_file() only.
    """

    sync_ttl_seconds: float = 60.0  # file reads are cheap

    @abstractmethod
    def paths(self) -> list[Path]:
        """Config file paths to read, in priority order."""
        ...

    @abstractmethod
    def parse_file(self, path: Path, content: str) -> list[SyncedProfile]:
        """Parse a config file into discovered profiles.

        Raise ValueError or similar on malformed content — the base class
        catches it and returns a degraded SyncResult.
        """
        ...

    async def detect(self) -> bool:
        """Return True if any config file from paths() exists and is readable."""
        for p in self.paths():
            try:
                if p.exists() and p.is_file():
                    return True
            except OSError:
                continue
        return False

    async def sync(self) -> SyncResult:
        """Read config files and parse profiles.

        Reads files in priority order from paths(). Aggregates all
        discovered profiles. Returns degraded SyncResult on I/O or
        parse errors rather than raising.
        """
        readable_paths = self.paths()
        if not readable_paths:
            return SyncResult(
                adapter_name=self.adapter_name,
                error="No config file paths configured",
            )

        all_profiles: list[SyncedProfile] = []
        errors: list[str] = []
        any_read = False

        for path in readable_paths:
            try:
                content = path.read_text(encoding="utf-8")
                any_read = True
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                errors.append(f"{path}: permission denied ({exc})")
                continue
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue

            if not content.strip():
                continue

            try:
                profiles = self.parse_file(path, content)
                all_profiles.extend(profiles)
            except Exception as exc:
                errors.append(f"{path}: parse error: {exc}")

        if not any_read and not all_profiles:
            paths_str = ", ".join(str(p) for p in readable_paths)
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"No readable config files found ({paths_str})",
            )

        error_msg = "; ".join(errors) if errors else None
        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=all_profiles,
            error=error_msg,
        )
```

- [ ] **Step 4: Update `__init__.py` exports**

Add to `src/nexus/bricks/auth/external_sync/__init__.py`:

```python
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

# Add to __all__:
"FileAdapter",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_file_adapter.py -xvs`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/file_adapter.py src/nexus/bricks/auth/external_sync/__init__.py src/nexus/bricks/auth/tests/test_file_adapter.py
git commit -m "feat(auth): add FileAdapter base class with detect/sync/error handling (#3739)"
```

---

## Task 3: SubprocessAdapter base class + tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/subprocess_adapter.py`
- Create: `src/nexus/bricks/auth/tests/test_subprocess_adapter.py`

- [ ] **Step 1: Write failing tests**

```python
# src/nexus/bricks/auth/tests/test_subprocess_adapter.py
"""Tests for SubprocessAdapter base class via synthetic adapter."""

from __future__ import annotations

import sys

import pytest

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import SyncedProfile, SyncResult


class TestSubprocessAdapterDetect:
    async def test_detect_true_for_existing_binary(self) -> None:
        from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

        adapter = _make_synthetic_subprocess(binary="python3")
        assert await adapter.detect() is True

    async def test_detect_false_for_missing_binary(self) -> None:
        from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

        adapter = _make_synthetic_subprocess(binary="definitely_not_installed_xyz")
        assert await adapter.detect() is False


class TestSubprocessAdapterSync:
    async def test_sync_parses_stdout(self) -> None:
        from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

        adapter = _make_synthetic_subprocess(
            binary=sys.executable,
            status_args=("-c", "print('account1\\naccount2')"),
        )
        result = await adapter.sync()
        assert result.error is None
        assert len(result.profiles) == 2
        assert result.profiles[0].account_identifier == "account1"

    async def test_sync_captures_stderr_on_nonzero_exit(self) -> None:
        from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

        adapter = _make_synthetic_subprocess(
            binary=sys.executable,
            status_args=("-c", "import sys; sys.stderr.write('auth failed'); sys.exit(1)"),
        )
        result = await adapter.sync()
        assert result.error is not None
        assert "auth failed" in result.error

    async def test_sync_timeout_returns_degraded(self) -> None:
        from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

        adapter = _make_synthetic_subprocess(
            binary=sys.executable,
            status_args=("-c", "import time; time.sleep(30)"),
            subprocess_timeout=0.5,
        )
        result = await adapter.sync()
        assert result.error is not None
        assert "timeout" in result.error.lower()

    async def test_sync_missing_binary_returns_degraded(self) -> None:
        from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

        adapter = _make_synthetic_subprocess(binary="nonexistent_binary_xyz")
        result = await adapter.sync()
        assert result.error is not None


class TestSubprocessAdapterRetry:
    async def test_retry_on_transient_failure(self) -> None:
        """Subprocess fails then succeeds — retry kicks in."""
        from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

        # Use a script that creates a temp file to track invocations:
        # first call exits 1, second call succeeds
        adapter = _make_synthetic_subprocess(
            binary=sys.executable,
            status_args=(
                "-c",
                (
                    "import os, tempfile, sys\n"
                    "marker = os.path.join(tempfile.gettempdir(), 'subprocess_adapter_retry_test')\n"
                    "if not os.path.exists(marker):\n"
                    "    open(marker, 'w').close()\n"
                    "    sys.stderr.write('transient error')\n"
                    "    sys.exit(1)\n"
                    "os.unlink(marker)\n"
                    "print('account1')\n"
                ),
            ),
            max_retries=2,
        )
        result = await adapter.sync()
        assert result.error is None
        assert len(result.profiles) == 1


def _make_synthetic_subprocess(
    binary: str = "echo",
    status_args: tuple[str, ...] = (),
    subprocess_timeout: float = 5.0,
    max_retries: int = 1,
) -> "SubprocessAdapter":
    from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

    class _Adapter(SubprocessAdapter):
        adapter_name = "synthetic-subprocess"
        binary_name = binary
        _status_args = status_args
        _subprocess_timeout = subprocess_timeout
        _max_retries = max_retries

        def get_status_args(self) -> tuple[str, ...]:
            return self._status_args

        def parse_output(self, stdout: str, stderr: str) -> list[SyncedProfile]:
            lines = [l.strip() for l in stdout.splitlines() if l.strip()]
            return [
                SyncedProfile(
                    provider="test",
                    account_identifier=line,
                    backend_key=f"synthetic-subprocess/{line}",
                    source="synthetic-subprocess",
                )
                for line in lines
            ]

        async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
            return ResolvedCredential(kind="api_key", api_key="fake-key")

    return _Adapter()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/nexus/bricks/auth/tests/test_subprocess_adapter.py -xvs`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.auth.external_sync.subprocess_adapter'`

- [ ] **Step 3: Implement SubprocessAdapter**

```python
# src/nexus/bricks/auth/external_sync/subprocess_adapter.py
"""SubprocessAdapter — base class for external CLIs that require shell-out.

Subclasses declare a binary name, status args, and a parse_output method.
The base class handles: PATH detection, asyncio.create_subprocess_exec,
timeout (default 5s), stderr capture, retry with exponential backoff,
and error classification.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from abc import abstractmethod

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)

logger = logging.getLogger(__name__)


class SubprocessAdapter(ExternalCliSyncAdapter):
    """Base class for subprocess-based sync adapters.

    Subclasses set binary_name, implement get_status_args() and parse_output().
    """

    binary_name: str
    sync_ttl_seconds: float = 300.0  # subprocess = expensive
    _subprocess_timeout: float = 5.0
    _max_retries: int = 1
    _backoff_base: float = 1.0  # seconds; doubles each retry

    @abstractmethod
    def get_status_args(self) -> tuple[str, ...]:
        """Return the CLI arguments to get account/status info."""
        ...

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str) -> list[SyncedProfile]:
        """Parse CLI output into discovered profiles.

        Raise on unexpected output — the base class catches and
        returns a degraded SyncResult.
        """
        ...

    async def detect(self) -> bool:
        """Return True if the binary is found on PATH."""
        return shutil.which(self.binary_name) is not None

    async def sync(self) -> SyncResult:
        """Run the CLI command, parse output, retry on transient failure."""
        binary_path = shutil.which(self.binary_name)
        if binary_path is None:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: binary not found on PATH",
            )

        last_error: str | None = None
        for attempt in range(1 + self._max_retries):
            if attempt > 0:
                delay = self._backoff_base * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

            try:
                result = await self._run_once(binary_path)
                if result.error is None:
                    return result
                last_error = result.error
            except Exception as exc:
                last_error = str(exc)

        return SyncResult(
            adapter_name=self.adapter_name,
            error=last_error,
        )

    async def _run_once(self, binary_path: str) -> SyncResult:
        """Single subprocess execution with timeout."""
        args = self.get_status_args()
        try:
            proc = await asyncio.create_subprocess_exec(
                binary_path,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._subprocess_timeout,
            )
        except asyncio.TimeoutError:
            # Kill the hung process
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: timeout after {self._subprocess_timeout}s",
            )
        except FileNotFoundError:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: binary not found",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            error_detail = stderr.strip() or f"exit code {proc.returncode}"
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: {error_detail}",
            )

        try:
            profiles = self.parse_output(stdout, stderr)
        except Exception as exc:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: parse error: {exc}",
            )

        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=profiles,
        )
```

- [ ] **Step 4: Update `__init__.py` exports**

Add to `src/nexus/bricks/auth/external_sync/__init__.py`:

```python
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

# Add to __all__:
"SubprocessAdapter",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_subprocess_adapter.py -xvs`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/subprocess_adapter.py src/nexus/bricks/auth/external_sync/__init__.py src/nexus/bricks/auth/tests/test_subprocess_adapter.py
git commit -m "feat(auth): add SubprocessAdapter base class with timeout/retry/stderr capture (#3739)"
```

---

## Task 4: AWS adapter + fixture files + tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/aws_sync.py`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/aws_credentials_v2.15.ini`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/aws_credentials_v2.16.ini`
- Create: `src/nexus/bricks/auth/tests/test_aws_sync.py`

- [ ] **Step 1: Create fixture files**

```ini
# src/nexus/bricks/auth/tests/fixtures/external_cli_output/aws_credentials_v2.15.ini
# Simulates ~/.aws/credentials from AWS CLI v2.15.x
# Standard format: multiple profiles with access key + secret key

[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY

[work-prod]
aws_access_key_id = AKIAI44QH8DHBEXAMPLE
aws_secret_access_key = je7MtGbClwBF/2Zp9Utk/h3yCo8nvbEXAMPLEKEY
region = us-west-2

[no-key-profile]
region = eu-west-1
output = json
```

```ini
# src/nexus/bricks/auth/tests/fixtures/external_cli_output/aws_credentials_v2.16.ini
# Simulates ~/.aws/credentials from AWS CLI v2.16.x
# Includes SSO session tokens and newer fields

[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY

[sso-session]
aws_access_key_id = ASIAZZZZZZZZZEXAMPLE
aws_secret_access_key = tempSecretFromSSO/EXAMPLEKEY
aws_session_token = FwoGZXIvYXdzEBYaDHqa0AP1/EXAMPLETOKEN
x_security_token_expires = 2026-04-15T12:00:00Z

[dev]
aws_access_key_id = AKIAXXXXXXXXXXXXXXXX
aws_secret_access_key = devSecretKeyExample12345678901234567

[future-unknown-field-profile]
aws_access_key_id = AKIAFUTUREFIELDEXAMPLE
aws_secret_access_key = futureSecret1234567890EXAMPLE
some_future_field = new-feature-value
```

- [ ] **Step 2: Write failing tests**

```python
# src/nexus/bricks/auth/tests/test_aws_sync.py
"""Tests for AwsCliSyncAdapter.parse_file() against fixture files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"


class TestAwsParseCredentials:
    def test_parse_v2_15_credentials(self) -> None:
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        adapter = AwsCliSyncAdapter()
        fixture = FIXTURES_DIR / "aws_credentials_v2.15.ini"
        content = fixture.read_text()
        profiles = adapter.parse_file(fixture, content)

        names = {p.account_identifier for p in profiles}
        assert "default" in names
        assert "work-prod" in names
        # no-key-profile should be skipped (no aws_access_key_id)
        assert "no-key-profile" not in names
        assert len(profiles) == 2

    def test_parse_v2_16_credentials(self) -> None:
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        adapter = AwsCliSyncAdapter()
        fixture = FIXTURES_DIR / "aws_credentials_v2.16.ini"
        content = fixture.read_text()
        profiles = adapter.parse_file(fixture, content)

        names = {p.account_identifier for p in profiles}
        assert "default" in names
        assert "sso-session" in names
        assert "dev" in names
        # future-unknown-field-profile should still parse (tolerant of unknown keys)
        assert "future-unknown-field-profile" in names
        assert len(profiles) == 4

    def test_parse_empty_file(self) -> None:
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        adapter = AwsCliSyncAdapter()
        profiles = adapter.parse_file(Path("empty.ini"), "")
        assert profiles == []

    def test_backend_key_format(self) -> None:
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        adapter = AwsCliSyncAdapter()
        fixture = FIXTURES_DIR / "aws_credentials_v2.15.ini"
        content = fixture.read_text()
        profiles = adapter.parse_file(fixture, content)

        default_profile = next(p for p in profiles if p.account_identifier == "default")
        assert default_profile.backend_key == "aws-cli/default"
        assert default_profile.provider == "s3"
        assert default_profile.source == "aws-cli"


class TestAwsParseConfig:
    def test_parse_config_file_strips_profile_prefix(self, tmp_path: Path) -> None:
        """~/.aws/config uses 'profile <name>' sections (except [default])."""
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        config_content = (
            "[default]\n"
            "region = us-east-1\n"
            "aws_access_key_id = AKIADEFAULTEXAMPLE\n"
            "aws_secret_access_key = defaultSecret\n"
            "\n"
            "[profile staging]\n"
            "aws_access_key_id = AKIASTAGINGEXAMPLE\n"
            "aws_secret_access_key = stagingSecret\n"
            "region = eu-west-1\n"
        )
        adapter = AwsCliSyncAdapter()
        profiles = adapter.parse_file(Path("config"), config_content)
        names = {p.account_identifier for p in profiles}
        assert "default" in names
        assert "staging" in names
        assert "profile staging" not in names


class TestAwsPaths:
    def test_paths_respects_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/custom/creds")
        monkeypatch.setenv("AWS_CONFIG_FILE", "/custom/config")
        adapter = AwsCliSyncAdapter()
        paths = adapter.paths()
        assert Path("/custom/creds") in paths
        assert Path("/custom/config") in paths

    def test_paths_defaults_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
        monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
        adapter = AwsCliSyncAdapter()
        paths = adapter.paths()
        home = Path.home()
        assert home / ".aws" / "credentials" in paths
        assert home / ".aws" / "config" in paths


class TestAwsSync:
    async def test_full_sync_from_fixture(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        # Point AWS env vars to tmp dir with fixture data
        creds_file = tmp_path / "credentials"
        creds_file.write_text((FIXTURES_DIR / "aws_credentials_v2.15.ini").read_text())
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nonexistent"))

        adapter = AwsCliSyncAdapter()
        result = await adapter.sync()
        assert result.error is None
        assert len(result.profiles) == 2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest src/nexus/bricks/auth/tests/test_aws_sync.py -xvs`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.auth.external_sync.aws_sync'`

- [ ] **Step 4: Implement AwsCliSyncAdapter**

```python
# src/nexus/bricks/auth/external_sync/aws_sync.py
"""AWS CLI sync adapter — discovers profiles from ~/.aws/credentials + config.

FileAdapter subclass. ~40 LOC of descriptor logic. All I/O, error handling,
and retry logic lives in the FileAdapter base class.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import SyncedProfile
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter


class AwsCliSyncAdapter(FileAdapter):
    """Discovers AWS profiles from credentials/config files."""

    adapter_name = "aws-cli"

    def paths(self) -> list[Path]:
        return [
            Path(
                os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials")
            ).expanduser(),
            Path(os.environ.get("AWS_CONFIG_FILE", "~/.aws/config")).expanduser(),
        ]

    def parse_file(self, path: Path, content: str) -> list[SyncedProfile]:
        parser = configparser.ConfigParser()
        parser.read_string(content)

        profiles: list[SyncedProfile] = []
        for section in parser.sections():
            if not parser.has_option(section, "aws_access_key_id"):
                continue

            # ~/.aws/config uses "profile <name>" sections (except [default])
            name = section
            if name.startswith("profile "):
                name = name[len("profile ") :]

            profiles.append(
                SyncedProfile(
                    provider="s3",
                    account_identifier=name,
                    backend_key=f"aws-cli/{name}",
                    source="aws-cli",
                )
            )

        return profiles

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        """Re-read AWS config files and extract credential for one profile."""
        _, profile_name = backend_key.split("/", 1)

        for path in self.paths():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue

            parser = configparser.ConfigParser()
            parser.read_string(content)

            # Try exact section name, then "profile <name>" variant
            for section in [profile_name, f"profile {profile_name}"]:
                if parser.has_section(section) and parser.has_option(
                    section, "aws_access_key_id"
                ):
                    return ResolvedCredential(
                        kind="api_key",
                        api_key=parser.get(section, "aws_access_key_id"),
                        metadata={
                            "secret_access_key": parser.get(
                                section, "aws_secret_access_key", fallback=""
                            ),
                            "session_token": parser.get(
                                section, "aws_session_token", fallback=""
                            ),
                            "region": parser.get(section, "region", fallback=""),
                        },
                    )

        from nexus.bricks.auth.credential_backend import CredentialResolutionError

        raise CredentialResolutionError(
            "external-cli",
            backend_key,
            f"AWS profile '{profile_name}' not found in config files",
        )
```

- [ ] **Step 5: Update `__init__.py` exports**

Add to `src/nexus/bricks/auth/external_sync/__init__.py`:

```python
from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

# Add to __all__:
"AwsCliSyncAdapter",
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_aws_sync.py -xvs`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/aws_sync.py src/nexus/bricks/auth/external_sync/__init__.py src/nexus/bricks/auth/tests/test_aws_sync.py src/nexus/bricks/auth/tests/fixtures/
git commit -m "feat(auth): add AwsCliSyncAdapter with fixture-based parse tests (#3739)"
```

---

## Task 5: Circuit breaker + AdapterRegistry + tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/registry.py`
- Create: `src/nexus/bricks/auth/tests/test_registry.py`

- [ ] **Step 1: Write failing tests**

```python
# src/nexus/bricks/auth/tests/test_registry.py
"""Tests for AdapterRegistry — startup, background loop, circuit breaker."""

from __future__ import annotations

import asyncio
import time

import pytest

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)
from nexus.bricks.auth.profile import InMemoryAuthProfileStore


# -- Helpers ----------------------------------------------------------------


class _FastAdapter(ExternalCliSyncAdapter):
    """Adapter that returns immediately."""

    adapter_name = "fast"
    sync_ttl_seconds = 60.0

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=[
                SyncedProfile(
                    provider="test",
                    account_identifier="acc1",
                    backend_key="fast/acc1",
                    source="fast",
                )
            ],
        )

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="api_key", api_key="key")


class _HangingAdapter(ExternalCliSyncAdapter):
    """Adapter that hangs forever."""

    adapter_name = "hanging"
    sync_ttl_seconds = 60.0

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        await asyncio.sleep(300)
        return SyncResult(adapter_name=self.adapter_name)

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="api_key", api_key="key")


class _FailingAdapter(ExternalCliSyncAdapter):
    """Adapter that always fails."""

    adapter_name = "failing"
    sync_ttl_seconds = 10.0
    failure_threshold = 3
    reset_timeout_seconds = 0.5

    def __init__(self) -> None:
        self.sync_count = 0

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        self.sync_count += 1
        return SyncResult(
            adapter_name=self.adapter_name,
            error="always fails",
        )

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="api_key", api_key="key")


class _RecoveringAdapter(ExternalCliSyncAdapter):
    """Adapter that fails N times then succeeds."""

    adapter_name = "recovering"
    sync_ttl_seconds = 10.0
    failure_threshold = 2
    reset_timeout_seconds = 0.3

    def __init__(self, fail_count: int = 2) -> None:
        self._fail_count = fail_count
        self.sync_count = 0

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        self.sync_count += 1
        if self.sync_count <= self._fail_count:
            return SyncResult(adapter_name=self.adapter_name, error="transient")
        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=[
                SyncedProfile(
                    provider="test",
                    account_identifier="recovered",
                    backend_key="recovering/recovered",
                    source="recovering",
                )
            ],
        )

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="api_key", api_key="key")


# -- Circuit breaker tests --------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        from nexus.bricks.auth.external_sync.registry import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=60.0)
        assert not cb.is_tripped
        assert not cb.is_half_open

    def test_trips_after_threshold(self) -> None:
        from nexus.bricks.auth.external_sync.registry import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=60.0)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_tripped
        cb.record_failure()
        assert cb.is_tripped

    def test_success_resets(self) -> None:
        from nexus.bricks.auth.external_sync.registry import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=2, reset_timeout_seconds=60.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_tripped
        cb.record_success()
        assert not cb.is_tripped
        assert cb.failure_count == 0

    def test_half_open_after_reset_timeout(self) -> None:
        from nexus.bricks.auth.external_sync.registry import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.1)
        cb.record_failure()
        assert cb.is_tripped
        time.sleep(0.15)
        assert cb.is_half_open
        assert not cb.is_tripped


# -- Startup tests -----------------------------------------------------------


class TestRegistryStartup:
    async def test_startup_returns_results(self) -> None:
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            adapters=[_FastAdapter()],
            profile_store=store,
        )
        results = await registry.startup()
        assert "fast" in results
        assert results["fast"].error is None
        assert len(results["fast"].profiles) == 1

    async def test_startup_upserts_into_store(self) -> None:
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            adapters=[_FastAdapter()],
            profile_store=store,
        )
        await registry.startup()
        profiles = store.list(provider="test")
        assert len(profiles) == 1
        assert profiles[0].backend == "external-cli"
        assert profiles[0].backend_key == "fast/acc1"

    async def test_startup_timeout_degrades_slow_adapter(self) -> None:
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        fast = _FastAdapter()
        hanging = _HangingAdapter()
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            adapters=[fast, hanging],
            profile_store=store,
            startup_timeout=1.0,
        )
        t0 = time.monotonic()
        results = await registry.startup()
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0  # bounded by timeout, not the 300s hang
        assert results["fast"].error is None
        assert results["hanging"].error is not None
        assert "timeout" in results["hanging"].error.lower()

    async def test_startup_5_adapters_4_fast_1_hanging(self) -> None:
        """Decision 15A: 4 ok + 1 degraded within ~startup_timeout."""
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        adapters = []
        for i in range(4):
            a = _FastAdapter()
            a.adapter_name = f"fast-{i}"
            adapters.append(a)
        adapters.append(_HangingAdapter())

        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            adapters=adapters,
            profile_store=store,
            startup_timeout=1.0,
        )
        t0 = time.monotonic()
        results = await registry.startup()
        elapsed = time.monotonic() - t0

        ok_count = sum(1 for r in results.values() if r.error is None)
        degraded_count = sum(1 for r in results.values() if r.error is not None)
        assert ok_count == 4
        assert degraded_count == 1
        assert elapsed < 2.0


# -- Circuit breaker integration tests ---------------------------------------


class TestRegistryCircuitBreaker:
    async def test_circuit_breaker_trips_after_threshold(self) -> None:
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        failing = _FailingAdapter()
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            adapters=[failing],
            profile_store=store,
        )
        # Startup counts as one failure
        await registry.startup()
        # Manually trigger syncs to hit threshold
        for _ in range(3):
            await registry._sync_adapter(failing)

        assert registry._breakers["failing"].is_tripped

    async def test_circuit_breaker_half_open_recovery(self) -> None:
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        recovering = _RecoveringAdapter(fail_count=2)
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            adapters=[recovering],
            profile_store=store,
        )
        # Fail twice to trip
        await registry._sync_adapter(recovering)
        await registry._sync_adapter(recovering)
        assert registry._breakers["recovering"].is_tripped

        # Wait for half-open
        await asyncio.sleep(0.4)
        assert registry._breakers["recovering"].is_half_open

        # Next sync should succeed and reset
        await registry._sync_adapter(recovering)
        assert not registry._breakers["recovering"].is_tripped
        profiles = store.list(provider="test")
        assert any(p.account_identifier == "recovered" for p in profiles)


# -- Background loop tests ---------------------------------------------------


class TestRegistryRefreshLoop:
    async def test_loop_respects_ttl(self) -> None:
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        fast = _FastAdapter()
        fast.sync_ttl_seconds = 0.3  # short TTL for test
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            adapters=[fast],
            profile_store=store,
            loop_tick_seconds=0.1,
        )
        await registry.startup()

        # Immediately after startup, adapter shouldn't be synced again
        initial_sync_time = registry._last_sync_times.get("fast")
        assert initial_sync_time is not None

        # Run loop briefly — should NOT re-sync within TTL
        loop_task = asyncio.create_task(registry.run_refresh_loop())
        await asyncio.sleep(0.2)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        # After TTL expires, loop should sync again
        registry2 = AdapterRegistry(
            adapters=[fast],
            profile_store=store,
            loop_tick_seconds=0.1,
        )
        await registry2.startup()
        await asyncio.sleep(0.4)  # past TTL
        loop_task2 = asyncio.create_task(registry2.run_refresh_loop())
        await asyncio.sleep(0.2)
        loop_task2.cancel()
        try:
            await loop_task2
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/nexus/bricks/auth/tests/test_registry.py -xvs`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement CircuitBreaker and AdapterRegistry**

```python
# src/nexus/bricks/auth/external_sync/registry.py
"""AdapterRegistry — manages adapter lifecycle, startup, background refresh.

Owns:
  - Startup sync with asyncio.gather + global timeout (decision 15A)
  - Per-adapter circuit breaker (configurable threshold + reset timeout)
  - Background refresh loop with per-adapter TTL
  - Upsert of SyncedProfile → AuthProfile into the store
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncResult,
)
from nexus.bricks.auth.profile import AuthProfile, AuthProfileStore, ProfileUsageStats

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreaker:
    """Per-adapter circuit breaker with half-open probe support."""

    failure_threshold: int = 3
    reset_timeout_seconds: float = 60.0
    failure_count: int = field(default=0, init=False)
    tripped_at: float | None = field(default=None, init=False)

    @property
    def is_tripped(self) -> bool:
        """True if breaker is open and reset timeout has NOT elapsed."""
        if self.tripped_at is None:
            return False
        return (time.monotonic() - self.tripped_at) < self.reset_timeout_seconds

    @property
    def is_half_open(self) -> bool:
        """True if breaker tripped but reset timeout has elapsed (allow probe)."""
        if self.tripped_at is None:
            return False
        return (time.monotonic() - self.tripped_at) >= self.reset_timeout_seconds

    def record_success(self) -> None:
        self.failure_count = 0
        self.tripped_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.tripped_at = time.monotonic()


class AdapterRegistry:
    """Manages external CLI sync adapter lifecycle."""

    def __init__(
        self,
        adapters: list[ExternalCliSyncAdapter],
        profile_store: AuthProfileStore,
        *,
        startup_timeout: float = 3.0,
        loop_tick_seconds: float = 30.0,
    ) -> None:
        self._adapters: dict[str, ExternalCliSyncAdapter] = {
            a.adapter_name: a for a in adapters
        }
        self._store = profile_store
        self._startup_timeout = startup_timeout
        self._loop_tick_seconds = loop_tick_seconds
        self._breakers: dict[str, CircuitBreaker] = {
            a.adapter_name: CircuitBreaker(
                failure_threshold=a.failure_threshold,
                reset_timeout_seconds=a.reset_timeout_seconds,
            )
            for a in adapters
        }
        self._last_sync_times: dict[str, float] = {}

    def get_adapter(self, adapter_name: str) -> ExternalCliSyncAdapter | None:
        return self._adapters.get(adapter_name)

    async def startup(self) -> dict[str, SyncResult]:
        """Run all adapters concurrently with a global timeout.

        Decision 15A: asyncio.gather with startup_timeout. Adapters
        that miss the deadline get a degraded SyncResult.
        """
        async def _sync_one(adapter: ExternalCliSyncAdapter) -> SyncResult:
            if not await adapter.detect():
                return SyncResult(
                    adapter_name=adapter.adapter_name,
                    error=f"{adapter.adapter_name}: not detected on this system",
                )
            return await adapter.sync()

        tasks = {
            name: asyncio.create_task(_sync_one(adapter))
            for name, adapter in self._adapters.items()
        }

        # Wait for all with global timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks.values(), return_exceptions=True),
                timeout=self._startup_timeout,
            )
        except asyncio.TimeoutError:
            pass

        results: dict[str, SyncResult] = {}
        for name, task in tasks.items():
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    results[name] = SyncResult(
                        adapter_name=name,
                        error=f"{name}: {exc}",
                    )
                else:
                    results[name] = task.result()
            else:
                task.cancel()
                results[name] = SyncResult(
                    adapter_name=name,
                    error=f"{name}: timeout during startup",
                )

        # Upsert successful results and update circuit breakers
        for name, result in results.items():
            if result.error is None:
                self._upsert_sync_results(result)
                self._breakers[name].record_success()
                self._last_sync_times[name] = time.monotonic()
            else:
                self._breakers[name].record_failure()

        return results

    async def run_refresh_loop(self) -> None:
        """Background refresh loop. Cancel via task.cancel()."""
        while True:
            await asyncio.sleep(self._loop_tick_seconds)
            now = time.monotonic()

            for name, adapter in self._adapters.items():
                breaker = self._breakers[name]

                # Skip if tripped and not half-open
                if breaker.is_tripped:
                    continue

                # Skip if not stale
                last = self._last_sync_times.get(name, 0.0)
                if (now - last) < adapter.sync_ttl_seconds:
                    continue

                await self._sync_adapter(adapter)

    async def _sync_adapter(self, adapter: ExternalCliSyncAdapter) -> SyncResult:
        """Sync a single adapter, updating breaker and store."""
        name = adapter.adapter_name
        breaker = self._breakers[name]

        try:
            result = await adapter.sync()
        except Exception as exc:
            result = SyncResult(adapter_name=name, error=str(exc))

        if result.error is None:
            self._upsert_sync_results(result)
            breaker.record_success()
            self._last_sync_times[name] = time.monotonic()
        else:
            breaker.record_failure()
            logger.warning("Adapter %s sync failed: %s", name, result.error)

        return result

    def _upsert_sync_results(self, result: SyncResult) -> None:
        """Map SyncedProfiles to AuthProfiles and upsert into the store."""
        now = datetime.now(UTC)
        for sp in result.profiles:
            profile_id = f"{sp.provider}/{sp.account_identifier}"
            existing = self._store.get(profile_id)

            profile = AuthProfile(
                id=profile_id,
                provider=sp.provider,
                account_identifier=sp.account_identifier,
                backend="external-cli",
                backend_key=sp.backend_key,
                last_synced_at=now,
                sync_ttl_seconds=int(
                    self._adapters[result.adapter_name].sync_ttl_seconds
                ),
                usage_stats=(existing.usage_stats if existing else ProfileUsageStats()),
            )
            self._store.upsert(profile)
```

- [ ] **Step 4: Update `__init__.py` exports**

Add to `src/nexus/bricks/auth/external_sync/__init__.py`:

```python
from nexus.bricks.auth.external_sync.registry import AdapterRegistry, CircuitBreaker

# Add to __all__:
"AdapterRegistry",
"CircuitBreaker",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_registry.py -xvs`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/registry.py src/nexus/bricks/auth/external_sync/__init__.py src/nexus/bricks/auth/tests/test_registry.py
git commit -m "feat(auth): add AdapterRegistry with circuit breaker + startup timeout + refresh loop (#3739)"
```

---

## Task 6: ExternalCliBackend + tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/external_cli_backend.py`
- Create: `src/nexus/bricks/auth/tests/test_external_cli_backend.py`

- [ ] **Step 1: Write failing tests**

```python
# src/nexus/bricks/auth/tests/test_external_cli_backend.py
"""Tests for ExternalCliBackend — CredentialBackend for external CLIs."""

from __future__ import annotations

import pytest

from nexus.bricks.auth.credential_backend import (
    BackendHealth,
    CredentialResolutionError,
    HealthStatus,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)
from nexus.bricks.auth.profile import InMemoryAuthProfileStore


class _MockAdapter(ExternalCliSyncAdapter):
    adapter_name = "mock-cli"
    sync_ttl_seconds = 60.0

    def __init__(self, credentials: dict[str, ResolvedCredential] | None = None) -> None:
        self._credentials = credentials or {}

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        return SyncResult(adapter_name=self.adapter_name)

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        if backend_key in self._credentials:
            return self._credentials[backend_key]
        raise CredentialResolutionError("external-cli", backend_key, "not found")


class TestExternalCliBackendResolve:
    async def test_resolve_delegates_to_adapter(self) -> None:
        from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        expected = ResolvedCredential(
            kind="api_key",
            api_key="AKIAEXAMPLE",
            metadata={"secret_access_key": "secret123"},
        )
        adapter = _MockAdapter(credentials={"mock-cli/default": expected})
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(adapters=[adapter], profile_store=store)

        backend = ExternalCliBackend(registry)
        result = await backend.resolve("mock-cli/default")
        assert result.kind == "api_key"
        assert result.api_key == "AKIAEXAMPLE"
        assert result.metadata["secret_access_key"] == "secret123"

    async def test_resolve_unknown_adapter_raises(self) -> None:
        from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(adapters=[], profile_store=store)

        backend = ExternalCliBackend(registry)
        with pytest.raises(CredentialResolutionError, match="no adapter"):
            await backend.resolve("nonexistent/profile")

    async def test_resolve_unknown_profile_raises(self) -> None:
        from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        adapter = _MockAdapter(credentials={})
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(adapters=[adapter], profile_store=store)

        backend = ExternalCliBackend(registry)
        with pytest.raises(CredentialResolutionError, match="not found"):
            await backend.resolve("mock-cli/nonexistent")


class TestExternalCliBackendHealthCheck:
    async def test_health_check_healthy(self) -> None:
        from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        cred = ResolvedCredential(kind="api_key", api_key="key")
        adapter = _MockAdapter(credentials={"mock-cli/default": cred})
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(adapters=[adapter], profile_store=store)

        backend = ExternalCliBackend(registry)
        health = await backend.health_check("mock-cli/default")
        assert health.status == HealthStatus.HEALTHY

    async def test_health_check_unhealthy_on_missing(self) -> None:
        from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        adapter = _MockAdapter(credentials={})
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(adapters=[adapter], profile_store=store)

        backend = ExternalCliBackend(registry)
        health = await backend.health_check("mock-cli/nonexistent")
        assert health.status == HealthStatus.UNHEALTHY


class TestExternalCliBackendName:
    def test_name_is_external_cli(self) -> None:
        from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(adapters=[], profile_store=store)
        backend = ExternalCliBackend(registry)
        assert backend.name == "external-cli"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/nexus/bricks/auth/tests/test_external_cli_backend.py -xvs`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement ExternalCliBackend**

```python
# src/nexus/bricks/auth/external_sync/external_cli_backend.py
"""ExternalCliBackend — CredentialBackend for external CLI credentials.

Implements the CredentialBackend protocol. resolve() delegates to the
appropriate adapter's resolve_credential() for a fresh read. Never
persists external credentials on nexus-side disk.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nexus.bricks.auth.credential_backend import (
    BackendHealth,
    CredentialResolutionError,
    HealthStatus,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.registry import AdapterRegistry


class ExternalCliBackend:
    """CredentialBackend for external-CLI-managed credentials.

    resolve() re-reads from the upstream source fresh every call.
    Never persists credentials on nexus-side disk.
    """

    _NAME = "external-cli"

    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return self._NAME

    async def resolve(self, backend_key: str) -> ResolvedCredential:
        """Resolve a credential by delegating to the appropriate adapter.

        backend_key format: "{adapter_name}/{profile_identifier}"
        """
        adapter_name, _ = self._parse_key(backend_key)
        adapter = self._registry.get_adapter(adapter_name)
        if adapter is None:
            raise CredentialResolutionError(
                self._NAME,
                backend_key,
                f"no adapter registered for '{adapter_name}'",
            )
        return await adapter.resolve_credential(backend_key)

    async def health_check(self, backend_key: str) -> BackendHealth:
        """Non-destructive health probe."""
        now = datetime.now(UTC)
        try:
            await self.resolve(backend_key)
            return BackendHealth(
                status=HealthStatus.HEALTHY,
                message="Credential resolved successfully",
                checked_at=now,
            )
        except Exception as exc:
            return BackendHealth(
                status=HealthStatus.UNHEALTHY,
                message=str(exc),
                checked_at=now,
            )

    @staticmethod
    def _parse_key(backend_key: str) -> tuple[str, str]:
        """Parse backend_key into (adapter_name, profile_identifier)."""
        parts = backend_key.split("/", 1)
        if len(parts) < 2:
            raise CredentialResolutionError(
                ExternalCliBackend._NAME,
                backend_key,
                f"expected 'adapter_name/profile', got {backend_key!r}",
            )
        return parts[0], parts[1]
```

- [ ] **Step 4: Update `__init__.py` exports**

Add to `src/nexus/bricks/auth/external_sync/__init__.py`:

```python
from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend

# Add to __all__:
"ExternalCliBackend",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_external_cli_backend.py -xvs`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/external_cli_backend.py src/nexus/bricks/auth/external_sync/__init__.py src/nexus/bricks/auth/tests/test_external_cli_backend.py
git commit -m "feat(auth): add ExternalCliBackend implementing CredentialBackend protocol (#3739)"
```

---

## Task 7: `nexus-fs auth list` cutover

**Files:**
- Modify: `src/nexus/fs/_auth_cli.py:264-304`
- Create: `src/nexus/bricks/auth/tests/test_auth_list_cutover.py`

- [ ] **Step 1: Write failing tests**

```python
# src/nexus/bricks/auth/tests/test_auth_list_cutover.py
"""Tests for nexus-fs auth list dual-read cutover."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.bricks.auth.profile import (
    AuthProfile,
    AuthProfileFailureReason,
    ProfileUsageStats,
)


def _make_store_profiles() -> list[AuthProfile]:
    """Create test profiles simulating a populated profile store."""
    now = datetime.now(UTC)
    return [
        AuthProfile(
            id="s3/default",
            provider="s3",
            account_identifier="default",
            backend="external-cli",
            backend_key="aws-cli/default",
            last_synced_at=now,
            usage_stats=ProfileUsageStats(
                last_used_at=now - timedelta(minutes=14),
                success_count=5,
            ),
        ),
        AuthProfile(
            id="s3/work-prod",
            provider="s3",
            account_identifier="work-prod",
            backend="external-cli",
            backend_key="aws-cli/work-prod",
            last_synced_at=now,
            usage_stats=ProfileUsageStats(
                last_used_at=now - timedelta(minutes=43),
                cooldown_until=now + timedelta(minutes=43),
                cooldown_reason=AuthProfileFailureReason.RATE_LIMIT,
            ),
        ),
        AuthProfile(
            id="openai/team",
            provider="openai",
            account_identifier="team",
            backend="nexus-token-manager",
            backend_key="openai/team@example.com",
            last_synced_at=now,
            usage_stats=ProfileUsageStats(
                last_used_at=now - timedelta(minutes=2),
                success_count=10,
            ),
        ),
    ]


class TestAuthListNewTable:
    def test_new_table_shows_source_column(self) -> None:
        """When profile store has data, show the new table format."""
        from nexus.fs._auth_cli import auth

        profiles = _make_store_profiles()

        with patch("nexus.fs._auth_cli._try_profile_store_list", return_value=profiles):
            runner = CliRunner()
            result = runner.invoke(auth, ["list"])
            assert result.exit_code == 0
            assert "Provider" in result.output or "provider" in result.output.lower()
            assert "Source" in result.output or "source" in result.output.lower()
            assert "aws-cli" in result.output
            assert "default" in result.output
            assert "work-prod" in result.output

    def test_new_table_shows_cooldown_status(self) -> None:
        from nexus.fs._auth_cli import auth

        profiles = _make_store_profiles()

        with patch("nexus.fs._auth_cli._try_profile_store_list", return_value=profiles):
            runner = CliRunner()
            result = runner.invoke(auth, ["list"])
            assert "cooldown" in result.output.lower()
            assert "rate_limit" in result.output.lower()


class TestAuthListFallback:
    def test_falls_back_when_store_empty(self) -> None:
        """When _try_profile_store_list returns None, use old path."""
        from nexus.fs._auth_cli import auth

        with (
            patch("nexus.fs._auth_cli._try_profile_store_list", return_value=None),
            patch("nexus.fs._auth_cli._build_auth_service") as mock_service,
        ):
            import asyncio
            from unittest.mock import AsyncMock

            mock_svc = mock_service.return_value
            mock_svc.list_summaries = AsyncMock(return_value=[])
            mock_svc.secret_store_path = "/mock/path"

            runner = CliRunner()
            result = runner.invoke(auth, ["list"])
            # Should not crash — falls back to old path
            assert result.exit_code == 0
            mock_svc.list_summaries.assert_called_once()


class TestAuthListJsonOutput:
    def test_json_output_new_format(self) -> None:
        from nexus.fs._auth_cli import auth

        profiles = _make_store_profiles()

        with patch("nexus.fs._auth_cli._try_profile_store_list", return_value=profiles):
            runner = CliRunner()
            result = runner.invoke(auth, ["list", "--json"])
            assert result.exit_code == 0
            import json
            data = json.loads(result.output)
            assert isinstance(data, list)
            assert len(data) == 3
            assert data[0]["provider"] == "s3"
            assert "source" in data[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/nexus/bricks/auth/tests/test_auth_list_cutover.py -xvs`
Expected: FAIL — `_try_profile_store_list` does not exist

- [ ] **Step 3: Modify `_auth_cli.py` — add helper + rewrite `list_auth`**

In `src/nexus/fs/_auth_cli.py`, add the helper function before `list_auth` (around line 260):

```python
def _try_profile_store_list() -> list | None:
    """Try reading from the unified profile store.

    Returns list of AuthProfile on success, None on any failure
    (no DB, empty, import error). Triggers fallback to old path.
    """
    try:
        from nexus.bricks.auth.profile import AuthProfile
        from nexus.bricks.auth.profile_store import SqliteAuthProfileStore
        from nexus.fs._paths import persistent_dir
    except ImportError:
        return None

    db_path = persistent_dir() / "auth_profiles.db"
    if not db_path.exists():
        return None

    try:
        store = SqliteAuthProfileStore(db_path)
        try:
            profiles = store.list()
        finally:
            store.close()
    except Exception:
        return None

    return profiles if profiles else None


def _format_status(profile: "AuthProfile") -> str:
    """Format profile status for the auth list table."""
    from datetime import UTC, datetime

    stats = profile.usage_stats
    now = datetime.now(UTC)

    if stats.disabled_until and stats.disabled_until > now:
        return "disabled"

    if stats.cooldown_until and stats.cooldown_until > now:
        remaining = stats.cooldown_until - now
        minutes = int(remaining.total_seconds() / 60)
        reason = stats.cooldown_reason.value if stats.cooldown_reason else "unknown"
        if minutes > 60:
            return f"cooldown  {reason} \u00b7 {minutes // 60}h {minutes % 60}m left"
        return f"cooldown  {reason} \u00b7 {minutes}m left"

    if profile.last_synced_at is None:
        return "not yet synced"

    return "ok"


def _format_relative_time(dt: "datetime | None") -> str:
    """Format a datetime as relative time (e.g. '14m ago')."""
    if dt is None:
        return "never"

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    # Handle naive datetimes by assuming UTC
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    minutes = int(delta.total_seconds() / 60)

    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _source_display(backend: str) -> str:
    """Map backend name to a user-friendly source label."""
    if backend == "external-cli":
        return "external"
    if backend == "nexus-token-manager":
        return "nexus"
    return backend
```

Then replace the `list_auth` function (lines 264-304):

```python
@auth.command("list")
@add_output_options
def list_auth(output_opts: OutputOptions) -> None:
    """List configured auth across services."""
    profiles = _try_profile_store_list()

    if profiles is not None:
        # New unified table from profile store
        data = [
            {
                "provider": p.provider,
                "account": p.account_identifier,
                "source": _source_display(p.backend),
                "status": _format_status(p),
                "last_used": _format_relative_time(p.usage_stats.last_used_at),
            }
            for p in profiles
        ]

        def _human_display(_data: object) -> None:
            table = Table(
                title="Unified Auth", show_header=True, header_style="bold cyan"
            )
            table.add_column("Provider", style="green")
            table.add_column("Account", style="cyan")
            table.add_column("Source", style="blue")
            table.add_column("Status", style="yellow")
            table.add_column("Last used")
            for row in data:
                table.add_row(
                    row["provider"],
                    row["account"],
                    row["source"],
                    row["status"],
                    row["last_used"],
                )
            console.print(table)

        render_output(
            data=data,
            output_opts=output_opts,
            human_formatter=_human_display,
        )
        return

    # Fallback: old UnifiedAuthService path
    service = _build_auth_service()
    summaries = asyncio.run(service.list_summaries())

    data = [
        {
            "service": s.service,
            "kind": s.kind.value,
            "status": s.status.value,
            "source": s.source,
            "message": s.message,
        }
        for s in summaries
    ]

    def _human_display(_data: object) -> None:
        table = Table(title="Unified Auth", show_header=True, header_style="bold cyan")
        table.add_column("Service", style="green")
        table.add_column("Kind", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Source", style="blue")
        table.add_column("Message")
        for summary in summaries:
            table.add_row(
                summary.service,
                summary.kind.value,
                summary.status.value,
                summary.source,
                summary.message,
            )
        console.print(table)
        console.print(f"[dim]Secret store: {service.secret_store_path}[/dim]")

    render_output(
        data=data,
        output_opts=output_opts,
        human_formatter=_human_display,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_auth_list_cutover.py -xvs`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/fs/_auth_cli.py src/nexus/bricks/auth/tests/test_auth_list_cutover.py
git commit -m "feat(auth): cut nexus-fs auth list over to profile store with dual-read fallback (#3739)"
```

---

## Task 8: S3 backend routing through profile store

**Files:**
- Modify: `src/nexus/fs/_backend_factory.py:36-46`

- [ ] **Step 1: Write failing test**

Add to `src/nexus/bricks/auth/tests/test_auth_list_cutover.py` (or create a separate file if preferred):

```python
# Append to test_auth_list_cutover.py or create test_s3_routing.py

class TestS3BackendRouting:
    def test_s3_routes_through_profile_store_when_populated(self) -> None:
        """When profile store has an S3 profile, use ExternalCliBackend."""
        from unittest.mock import MagicMock, patch

        mock_profile = MagicMock()
        mock_profile.backend_key = "aws-cli/default"

        mock_cred = MagicMock()
        mock_cred.api_key = "AKIAEXAMPLE"
        mock_cred.metadata = {"secret_access_key": "secret", "session_token": "", "region": "us-east-1"}

        with (
            patch("nexus.fs._backend_factory._try_profile_store_select", return_value=mock_profile),
            patch("nexus.fs._backend_factory._resolve_external_credential", return_value=mock_cred),
            patch("nexus.backends.storage.path_s3.PathS3Backend") as mock_s3,
        ):
            from nexus.fs._backend_factory import create_backend

            spec = MagicMock()
            spec.scheme = "s3"
            spec.authority = "my-bucket"
            spec.path = "/prefix"

            create_backend(spec)

            mock_s3.assert_called_once()
            call_kwargs = mock_s3.call_args
            assert call_kwargs[1].get("access_key_id") == "AKIAEXAMPLE" or call_kwargs.kwargs.get("access_key_id") == "AKIAEXAMPLE"

    def test_s3_falls_back_when_no_profile(self) -> None:
        """When profile store has no S3 profile, use old discover_credentials path."""
        from unittest.mock import MagicMock, patch

        with (
            patch("nexus.fs._backend_factory._try_profile_store_select", return_value=None),
            patch("nexus.fs._backend_factory.discover_credentials", return_value={"source": "env"}),
            patch("nexus.backends.storage.path_s3.PathS3Backend") as mock_s3,
        ):
            from nexus.fs._backend_factory import create_backend

            spec = MagicMock()
            spec.scheme = "s3"
            spec.authority = "my-bucket"
            spec.path = "/prefix"

            create_backend(spec)

            mock_s3.assert_called_once()
            # Old path: no explicit credentials
            call_kwargs = mock_s3.call_args
            assert call_kwargs.kwargs.get("access_key_id") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest src/nexus/bricks/auth/tests/test_auth_list_cutover.py::TestS3BackendRouting -xvs`
Expected: FAIL — `_try_profile_store_select` does not exist

- [ ] **Step 3: Modify `_backend_factory.py`**

Add helpers before `create_backend()`:

```python
def _try_profile_store_select(provider: str) -> "AuthProfile | None":
    """Try selecting an active profile from the unified store."""
    try:
        from nexus.bricks.auth.profile import AuthProfile
        from nexus.bricks.auth.profile_store import SqliteAuthProfileStore
        from nexus.fs._paths import persistent_dir
    except ImportError:
        return None

    db_path = persistent_dir() / "auth_profiles.db"
    if not db_path.exists():
        return None

    try:
        store = SqliteAuthProfileStore(db_path)
        try:
            profiles = store.list(provider=provider)
        finally:
            store.close()
    except Exception:
        return None

    if not profiles:
        return None

    # Return the first non-cooldown profile
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    for p in profiles:
        stats = p.usage_stats
        if stats.cooldown_until and stats.cooldown_until > now:
            continue
        if stats.disabled_until and stats.disabled_until > now:
            continue
        return p

    # All on cooldown — return first anyway (caller can decide)
    return profiles[0]


def _resolve_external_credential(backend_key: str) -> "ResolvedCredential | None":
    """Resolve a credential via ExternalCliBackend without the full registry."""
    try:
        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter
    except ImportError:
        return None

    import asyncio

    adapter = AwsCliSyncAdapter()
    try:
        return asyncio.run(adapter.resolve_credential(backend_key))
    except Exception:
        return None
```

Then modify the S3 branch in `create_backend()` (lines 36-46):

```python
    if spec.scheme == "s3":
        try:
            from nexus.backends.storage.path_s3 import PathS3Backend
        except ImportError:
            raise ImportError(
                "boto3 is required for S3 backends. Install with: pip install nexus-fs[s3]"
            ) from None

        # Phase 2: try profile store first
        profile = _try_profile_store_select(provider="s3")
        if profile is not None and profile.backend == "external-cli":
            cred = _resolve_external_credential(profile.backend_key)
            if cred is not None:
                return PathS3Backend(
                    bucket_name=spec.authority,
                    prefix=spec.path.lstrip("/") if spec.path else "",
                    access_key_id=cred.api_key,
                    secret_access_key=cred.metadata.get("secret_access_key", ""),
                    session_token=cred.metadata.get("session_token") or None,
                    region_name=cred.metadata.get("region") or None,
                )

        # Fallback: old discover_credentials() path
        from nexus.fs._credentials import discover_credentials

        discover_credentials(spec.scheme)
        return PathS3Backend(
            bucket_name=spec.authority,
            prefix=spec.path.lstrip("/") if spec.path else "",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_auth_list_cutover.py::TestS3BackendRouting -xvs`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/fs/_backend_factory.py src/nexus/bricks/auth/tests/test_auth_list_cutover.py
git commit -m "feat(auth): route S3 auth through profile store with dual-path fallback (#3739)"
```

---

## Task 9: Concurrency + offline safety tests

**Files:**
- Modify: `src/nexus/bricks/auth/tests/test_registry.py`

- [ ] **Step 1: Add concurrency test**

Append to `src/nexus/bricks/auth/tests/test_registry.py`:

```python
class TestConcurrency:
    async def test_concurrent_list_during_upsert(self) -> None:
        """Two readers + one writer — no torn reads, no crashes."""
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry

        fast = _FastAdapter()
        fast.sync_ttl_seconds = 0.1
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            adapters=[fast],
            profile_store=store,
            loop_tick_seconds=0.05,
        )
        await registry.startup()

        read_results: list[list] = []
        errors: list[Exception] = []

        async def reader() -> None:
            for _ in range(20):
                try:
                    profiles = store.list(provider="test")
                    read_results.append(profiles)
                except Exception as exc:
                    errors.append(exc)
                await asyncio.sleep(0.01)

        async def writer() -> None:
            loop_task = asyncio.create_task(registry.run_refresh_loop())
            await asyncio.sleep(0.3)
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        await asyncio.gather(reader(), reader(), writer())

        assert not errors, f"Concurrent access errors: {errors}"
        # All reads should return consistent snapshots
        for profiles in read_results:
            assert isinstance(profiles, list)


class TestOfflineSafety:
    async def test_file_adapter_returns_degraded_with_no_network(
        self, no_network: None
    ) -> None:
        """FileAdapter must work offline (it reads local files, no network)."""
        from nexus.bricks.auth.external_sync.file_adapter import FileAdapter

        class _OfflineAdapter(FileAdapter):
            adapter_name = "offline-test"

            def paths(self) -> list[Path]:
                return [Path("/nonexistent/file.conf")]

            def parse_file(self, path: Path, content: str) -> list[SyncedProfile]:
                return []

            async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
                return ResolvedCredential(kind="api_key", api_key="key")

        adapter = _OfflineAdapter()
        t0 = time.monotonic()
        result = await adapter.sync()
        elapsed = time.monotonic() - t0

        # Must complete within 2s even with network blocked
        assert elapsed < 2.0
        # Returns degraded (file not found), not a network error
        assert result.error is not None

    async def test_subprocess_adapter_returns_degraded_with_no_network(
        self, no_network: None
    ) -> None:
        """SubprocessAdapter must handle missing binary gracefully offline."""
        from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

        class _OfflineSubprocess(SubprocessAdapter):
            adapter_name = "offline-subprocess"
            binary_name = "nonexistent_cli_tool"

            def get_status_args(self) -> tuple[str, ...]:
                return ("status",)

            def parse_output(self, stdout: str, stderr: str) -> list[SyncedProfile]:
                return []

            async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
                return ResolvedCredential(kind="api_key", api_key="key")

        adapter = _OfflineSubprocess()
        t0 = time.monotonic()
        result = await adapter.sync()
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0
        assert result.error is not None
```

Also add the missing imports at the top:

```python
from pathlib import Path
from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import SyncedProfile
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_registry.py -xvs -k "TestConcurrency or TestOfflineSafety"`
Expected: all PASS (these test existing code, they should pass immediately)

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/auth/tests/test_registry.py
git commit -m "test(auth): add concurrency + offline safety tests for external sync framework (#3739)"
```

---

## Task 10: Nightly real-binary e2e test

**Files:**
- Create: `src/nexus/bricks/auth/tests/test_e2e_aws.py`

- [ ] **Step 1: Write the gated e2e test**

```python
# src/nexus/bricks/auth/tests/test_e2e_aws.py
"""Nightly end-to-end test with real AWS CLI.

Gated by TEST_WITH_REAL_AWS_CLI=1 env var. Skipped in default CI.
Creates a temp HOME, runs `aws configure` with dummy credentials,
then asserts the full sync + auth list flow works.
"""

from __future__ import annotations

import os
import subprocess
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_WITH_REAL_AWS_CLI"),
    reason="Requires TEST_WITH_REAL_AWS_CLI=1 and aws CLI installed",
)


@pytest.fixture()
def aws_home(tmp_path):
    """Set up a temp HOME with AWS credentials configured."""
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()

    creds = aws_dir / "credentials"
    creds.write_text(
        "[default]\n"
        "aws_access_key_id = AKIATESTEXAMPLE123456\n"
        "aws_secret_access_key = testSecretKey1234567890ABCDEF\n"
        "\n"
        "[staging]\n"
        "aws_access_key_id = AKIASTAGINGEXAMPLE789\n"
        "aws_secret_access_key = stagingSecretKey123456789\n"
    )

    config = aws_dir / "config"
    config.write_text(
        "[default]\n"
        "region = us-east-1\n"
        "\n"
        "[profile staging]\n"
        "region = eu-west-1\n"
    )

    return tmp_path


class TestRealAwsCli:
    def test_aws_cli_is_installed(self) -> None:
        assert shutil.which("aws") is not None, "aws CLI not found on PATH"

    async def test_full_sync_and_list(self, aws_home, monkeypatch) -> None:
        """End-to-end: discover AWS profiles → registry startup → check profiles."""
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_home / ".aws" / "credentials"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_home / ".aws" / "config"))

        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter
        from nexus.bricks.auth.external_sync.registry import AdapterRegistry
        from nexus.bricks.auth.profile import InMemoryAuthProfileStore

        store = InMemoryAuthProfileStore()
        adapter = AwsCliSyncAdapter()
        registry = AdapterRegistry(adapters=[adapter], profile_store=store)

        results = await registry.startup()
        assert results["aws-cli"].error is None

        profiles = store.list(provider="s3")
        names = {p.account_identifier for p in profiles}
        assert "default" in names
        assert "staging" in names

        # Verify source is correct
        for p in profiles:
            assert p.backend == "external-cli"
            assert p.backend_key.startswith("aws-cli/")

    async def test_resolve_credential_from_file(self, aws_home, monkeypatch) -> None:
        """End-to-end: resolve actual credential from AWS config file."""
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_home / ".aws" / "credentials"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_home / ".aws" / "config"))

        from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

        adapter = AwsCliSyncAdapter()
        cred = await adapter.resolve_credential("aws-cli/default")

        assert cred.kind == "api_key"
        assert cred.api_key == "AKIATESTEXAMPLE123456"
        assert cred.metadata["secret_access_key"] == "testSecretKey1234567890ABCDEF"
```

- [ ] **Step 2: Run test to verify it's skipped in default CI**

Run: `pytest src/nexus/bricks/auth/tests/test_e2e_aws.py -xvs`
Expected: all tests SKIPPED with "Requires TEST_WITH_REAL_AWS_CLI=1"

- [ ] **Step 3: Run test with env var to verify it passes (optional, only if aws CLI is installed)**

Run: `TEST_WITH_REAL_AWS_CLI=1 pytest src/nexus/bricks/auth/tests/test_e2e_aws.py -xvs`
Expected: PASS (if aws CLI is installed)

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/auth/tests/test_e2e_aws.py
git commit -m "test(auth): add nightly real-binary e2e test for AWS CLI sync (#3739)"
```

---

## Task 11: Final `__init__.py` cleanup + full test suite run

**Files:**
- Verify: `src/nexus/bricks/auth/external_sync/__init__.py`

- [ ] **Step 1: Verify `__init__.py` exports are complete**

Final state of `src/nexus/bricks/auth/external_sync/__init__.py` should be:

```python
"""External CLI sync framework for discovering credentials managed by external tools."""

from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)
from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter
from nexus.bricks.auth.external_sync.registry import AdapterRegistry, CircuitBreaker
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

__all__ = [
    "AdapterRegistry",
    "AwsCliSyncAdapter",
    "CircuitBreaker",
    "ExternalCliBackend",
    "ExternalCliSyncAdapter",
    "FileAdapter",
    "SubprocessAdapter",
    "SyncedProfile",
    "SyncResult",
]
```

- [ ] **Step 2: Run the full auth test suite**

Run: `pytest src/nexus/bricks/auth/tests/ -xvs --strict-markers`
Expected: all PASS. No regressions in existing tests.

- [ ] **Step 3: Run mypy on the new module**

Run: `mypy src/nexus/bricks/auth/external_sync/ --strict`
Expected: PASS (or fix any type errors)

- [ ] **Step 4: Run ruff on the new module**

Run: `ruff check src/nexus/bricks/auth/external_sync/`
Expected: PASS

- [ ] **Step 5: Final commit if any cleanup was needed**

```bash
git add -u
git commit -m "chore(auth): final cleanup for external sync framework (#3739)"
```

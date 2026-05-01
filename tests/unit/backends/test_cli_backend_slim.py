"""Slim-safety tests for CLI-backed connector primitives (Issue #3837)."""

from __future__ import annotations

import os
import subprocess
import sys
from textwrap import dedent

import pytest

_BLOCKED_RUNTIME_TREES = (
    "nexus.server",
    "nexus.bricks",
    "nexus.factory",
    "nexus.raft",
    "nexus.cli",
    "nexus.fuse",
    "nexus.remote",
    "nexus.services",
    "nexus.grpc",
    "nexus.security",
    "nexus.cache",
    "nexus.daemon",
    "nexus.migrations",
    "nexus.network",
    "nexus.plugins",
    "nexus.proxy",
    "nexus.sdk",
    "nexus.task_manager",
    "nexus.tasks",
    "nexus.tools",
    "nexus.validation",
)


def _run_slim_import_script(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_src_dir()), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        cwd=_repo_root(),
        env=env,
        text=True,
        capture_output=True,
    )


def _repo_root() -> str:
    return os.fspath(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))


def _src_dir() -> str:
    return os.path.join(_repo_root(), "src")


def _slim_import_blocker_prelude() -> str:
    return dedent(
        f"""
        import importlib.abc
        import sys

        BLOCKED = {_BLOCKED_RUNTIME_TREES!r}

        class BlockedSlimRuntime(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname in BLOCKED or any(
                    fullname.startswith(root + ".") for root in BLOCKED
                ):
                    raise ModuleNotFoundError(
                        f"blocked simulated slim nexus-fs wheel: {{fullname}}",
                        name=fullname,
                    )
                return None

        sys.meta_path.insert(0, BlockedSlimRuntime())
        """
    )


def test_base_cli_backend_imports_without_full_runtime() -> None:
    script = (
        _slim_import_blocker_prelude()
        + """
from nexus.backends.base.cli_backend import (
    DisplayPathMixin,
    PathCLIBackend,
    sanitize_filename,
)

assert PathCLIBackend.__module__ == "nexus.backends.base.cli_backend"
assert DisplayPathMixin().display_path("abc") == "abc.yaml"
assert sanitize_filename("feat: slim path?") == "feat-slim-path"
"""
    )

    result = _run_slim_import_script(script)

    assert result.returncode == 0, result.stderr


def test_builtin_cli_connectors_use_slim_safe_base_module() -> None:
    script = (
        _slim_import_blocker_prelude()
        + """
from nexus.backends.connectors.github.connector import GitHubConnector
from nexus.backends.connectors.gws.connector import GmailConnector

assert GmailConnector.__mro__[1].__module__ == "nexus.backends.base.cli_backend"
assert GitHubConnector.__mro__[1].__module__ == "nexus.backends.base.cli_backend"
"""
    )

    result = _run_slim_import_script(script)

    assert result.returncode == 0, result.stderr


def test_gws_mount_without_binary_surfaces_manifest_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.backends.base.factory import BackendFactory
    from nexus.contracts.exceptions import MissingDependencyError

    monkeypatch.setattr("shutil.which", lambda name: None if name == "gws" else None)

    with pytest.raises(MissingDependencyError) as exc_info:
        BackendFactory.create("gws_gmail", {})

    message = str(exc_info.value)
    assert "gws_gmail" in message
    assert "binary 'gws'" in message
    assert "brew install nexi-lab/tap/gws" in message

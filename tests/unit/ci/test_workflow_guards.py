"""Workflow-level guard tests.

These tests are not the SSOT for the workflow — the YAML files are.
They exist to catch silent regressions when someone reformats or
restructures the workflow without realising they broke an invariant.

Asserting against parsed YAML (structural assertions) lets the
workflow author rename steps, reorder steps, or reflow YAML whitespace
freely; the test only complains when the invariant itself is gone.
The previous version of this module asserted against substrings of
the raw text, which broke whenever the workflow file was touched even
when the invariant still held.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def _load_workflow(name: str) -> dict[str, Any]:
    # ``encoding="utf-8"`` is required: ``Path.read_text()`` defaults to
    # ``locale.getencoding()`` which is GBK on a zh-CN Windows host. Several
    # workflow files contain U+2014 ``—`` (em-dash) in comments / step
    # ``name:`` fields, and a GBK decode raises ``UnicodeDecodeError`` long
    # before ``yaml.safe_load`` ever sees the bytes. CI runners are Linux
    # (UTF-8 default), so the locale mismatch only bites local Windows runs.
    raw = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    # GitHub Actions reserves the ``on:`` key. ``yaml.safe_load`` parses it
    # as the Python boolean ``True`` by default. We don't read it in these
    # tests, but document the gotcha so future readers don't trip on it.
    return yaml.safe_load(raw)


def _job(workflow: dict[str, Any], job_id: str) -> dict[str, Any]:
    jobs = workflow.get("jobs", {})
    assert job_id in jobs, f"job {job_id!r} missing from workflow; available: {sorted(jobs)}"
    return jobs[job_id]


def _step_by_name(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job.get("steps", []):
        if step.get("name") == name:
            return step
    names = [s.get("name") for s in job.get("steps", [])]
    raise AssertionError(f"step {name!r} not found; have: {names}")


def _workflow_env(workflow: dict[str, Any]) -> dict[str, Any]:
    env = workflow.get("env") or {}
    assert isinstance(env, dict), f"workflow env must be a mapping, got: {env!r}"
    return env


def _assert_pyo3_forward_compat_enabled(workflow_name: str) -> None:
    workflow = _load_workflow(workflow_name)
    env = _workflow_env(workflow)
    assert env.get("PYO3_USE_ABI3_FORWARD_COMPATIBILITY") == "1", (
        f"{workflow_name} installs the project under Python 3.14. "
        "pdf-inspector may build its PyO3 sdist there, so the workflow "
        "must export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1."
    )


def _assert_cache_export_non_blocking(workflow_name: str, job_id: str, step_name: str) -> None:
    workflow = _load_workflow(workflow_name)
    step = _step_by_name(_job(workflow, job_id), step_name)
    with_block = step.get("with") or {}
    cache_to = with_block.get("cache-to", "")
    if not cache_to:
        return
    assert "ignore-error=true" in cache_to, (
        f"{workflow_name}:{step_name} must set ignore-error=true on cache-to. "
        "The image build/push is the required artifact; a cache export failure "
        "must not fail the job after the image is built."
    )


# ---------------------------------------------------------------------------
# docker-publish.yml
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["build-platform", "build-cluster-platform"])
def test_docker_publish_cache_export_is_non_blocking(job_id: str) -> None:
    _assert_cache_export_non_blocking(
        "docker-publish.yml",
        job_id,
        "Build and push by digest",
    )


def test_release_tag_promotion_requires_the_published_image_smoke_to_pass() -> None:
    workflow = _load_workflow("docker-publish.yml")
    promotion = _job(workflow, "promote-tags")
    assert "e2e-edge" in promotion["needs"]
    assert "always()" not in promotion.get("if", "")
    smoke = _job(workflow, "e2e-edge")
    assert set(smoke["needs"]) == {"build-and-push", "push-cluster-manifest"}
    assert not smoke.get("continue-on-error", False)


@pytest.mark.skipif(sys.platform == "win32", reason="unit-test workflow uses Unix bash")
@pytest.mark.parametrize("status", [0, 1, 124, 137, 143])
def test_unit_workflow_preserves_process_failure_after_a_passing_summary(tmp_path, status) -> None:
    step = _step_by_name(_job(_load_workflow("test.yml"), "test"), "Run unit tests")
    bash = shutil.which("bash")
    assert bash is not None
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "print('==================== 6000 passed ====================')\n"
        f"raise SystemExit({status})\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    result = subprocess.run(
        [bash, "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == status, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="unit-test workflow uses Unix bash")
def test_unit_workflow_reports_a_real_worker_crash_without_restarting_it(tmp_path) -> None:
    step = _step_by_name(_job(_load_workflow("test.yml"), "test"), "Run unit tests")
    bash = shutil.which("bash")
    assert bash is not None
    cases = tmp_path / "test_worker.py"
    cases.write_text(
        "def test_before(): pass\n"
        "def test_crash():\n"
        "    import os\n"
        "    os._exit(23)\n"
        "def test_after(): pass\n",
        encoding="utf-8",
    )
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[4:]\n"
        "args = [arg for arg in args if not arg.startswith('--cov')]\n"
        f"args[args.index('tests/unit')] = {str(cases)!r}\n"
        "args[args.index('auto')] = '2'\n"
        "args.extend(['-p', 'xdist.plugin', '-p', 'pytest_timeout'])\n"
        "os.execv(sys.executable, [sys.executable, *args])\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    result = subprocess.run(
        [bash, "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "crashed while running" in output and "test_crash" in output, output
    assert "replacing crashed worker" not in output, output


# ---------------------------------------------------------------------------
# docs.yml
# ---------------------------------------------------------------------------


def test_docs_workflow_exports_pyo3_forward_compatibility() -> None:
    _assert_pyo3_forward_compat_enabled("docs.yml")


# ---------------------------------------------------------------------------
# demo-memory.yml
# ---------------------------------------------------------------------------


def test_demo_memory_workflow_exports_pyo3_forward_compatibility() -> None:
    _assert_pyo3_forward_compat_enabled("demo-memory.yml")


def test_demo_memory_gha_cache_export_is_non_blocking() -> None:
    _assert_cache_export_non_blocking(
        "demo-memory.yml",
        "demo-memory",
        "Build nexus image",
    )

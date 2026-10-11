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

from pathlib import Path
from typing import Any

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


def _assert_gha_cache_export_non_blocking(workflow_name: str, job_id: str, step_name: str) -> None:
    workflow = _load_workflow(workflow_name)
    step = _step_by_name(_job(workflow, job_id), step_name)
    with_block = step.get("with") or {}
    cache_to = with_block.get("cache-to", "")
    assert "type=gha" in cache_to, f"{workflow_name}:{step_name} should export a GHA build cache"
    assert "ignore-error=true" in cache_to, (
        f"{workflow_name}:{step_name} must set ignore-error=true on cache-to. "
        "The image build/push is the required artifact; a transient GitHub "
        "Actions cache export failure must not fail the job after the image is built."
    )


# ---------------------------------------------------------------------------
# docker-publish.yml
# ---------------------------------------------------------------------------


def test_docker_publish_gha_cache_export_is_non_blocking() -> None:
    _assert_gha_cache_export_non_blocking(
        "docker-publish.yml",
        "build-platform",
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
    _assert_gha_cache_export_non_blocking(
        "demo-memory.yml",
        "demo-memory",
        "Build nexus image",
    )

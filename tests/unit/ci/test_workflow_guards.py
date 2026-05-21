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
# cluster-binary-build.yml
# ---------------------------------------------------------------------------


def test_cluster_binary_uses_single_rust_cache_layer() -> None:
    """Cluster binary build must let ``Swatinem/rust-cache`` be the only
    Rust cache.

    ``actions-rust-lang/setup-rust-toolchain`` enables its own cache by
    default; we layer ``Swatinem/rust-cache`` on top deliberately, so the
    toolchain step must opt out of its own cache to avoid double-caching
    (slow + wastes runner disk).
    """
    workflow = _load_workflow("cluster-binary-build.yml")
    # Single matrix-driven job; iterate it directly.
    job = _job(workflow, "build")

    toolchain_steps = [
        step
        for step in job.get("steps", [])
        if isinstance(step.get("uses"), str)
        and step["uses"].startswith("actions-rust-lang/setup-rust-toolchain")
    ]
    assert toolchain_steps, "Expected a setup-rust-toolchain step in cluster-binary build job"

    for step in toolchain_steps:
        with_block = step.get("with") or {}
        assert with_block.get("cache") is False, (
            f"setup-rust-toolchain step must set ``with.cache: false`` to avoid "
            f"double-caching with Swatinem/rust-cache. Got: with={with_block!r}"
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


_VFS_GRPC_GATE = "steps.vfs_grpc.outputs.available == 'true'"


def test_docker_edge_smoke_skips_grpc_dependent_steps_without_vfs_grpc() -> None:
    """Without a working VFS gRPC, the edge smoke job must skip gRPC-only
    steps instead of running them against a daemon that has no listener.

    Asserts the structural shape:
      1. A ``Check VFS gRPC availability`` step that exports a step output
         named ``available``.
      2. The two downstream steps that exercise gRPC (permissions demo and
         build perf e2e) gate on that output.
    """
    workflow = _load_workflow("docker-publish.yml")
    job = _job(workflow, "e2e-edge")

    probe = _step_by_name(job, "Check VFS gRPC availability")
    assert probe.get("id") == "vfs_grpc", (
        f"Probe step must have ``id: vfs_grpc`` for downstream ``if:`` "
        f"conditions to reference it. Got id={probe.get('id')!r}"
    )
    run_body = probe.get("run", "")
    assert "available=true" in run_body and "available=false" in run_body, (
        "Probe must emit both ``available=true`` and ``available=false`` "
        "via $GITHUB_OUTPUT — without both branches the gate is undefined."
    )

    permissions = _step_by_name(job, "Run permissions demo")
    build_perf = _step_by_name(job, "Run build perf e2e")
    for step in (permissions, build_perf):
        condition = step.get("if", "")
        assert _VFS_GRPC_GATE in condition, (
            f"step {step.get('name')!r} must gate on ``{_VFS_GRPC_GATE}``; "
            f"otherwise it will fail against an edge image without VFS gRPC. "
            f"Got: if={condition!r}"
        )


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

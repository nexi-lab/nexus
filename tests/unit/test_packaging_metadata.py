"""Regression tests for packaging metadata used by the source quickstart."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_semantic_search_stack_is_not_in_base_dependencies() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    base_dependencies = payload["project"]["dependencies"]
    semantic_search = payload["project"]["optional-dependencies"]["semantic-search"]

    assert not any(dep.startswith("txtai[database,graph]") for dep in base_dependencies)
    assert not any(dep.startswith("faiss-cpu") for dep in base_dependencies)
    assert any(dep.startswith("txtai[database,graph]") for dep in semantic_search)
    assert any(dep.startswith("faiss-cpu") for dep in semantic_search)


def test_root_package_does_not_advertise_unpublished_nexus_kernel_extra() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    optional = payload["project"]["optional-dependencies"]

    assert "rust" not in optional
    assert "fast" not in optional


def test_rust_package_versions_match_main_package() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    root_payload = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    # Phase 0 (refactor/rust-workspace-parallel-layers): the wheel-producing
    # crate moved from `rust/kernel/` to `rust/nexus-cdylib/`. Cargo
    # version stays sourced from kernel/Cargo.toml since both crates
    # ship the same version in lock-step.
    rust_payload = tomllib.loads(
        (repo_root / "rust" / "nexus-cdylib" / "pyproject.toml").read_text(encoding="utf-8")
    )

    cargo_version = None
    for line in (
        (repo_root / "rust" / "kernel" / "Cargo.toml").read_text(encoding="utf-8").splitlines()
    ):
        if line.startswith("version = "):
            cargo_version = line.split('"')[1]
            break

    assert cargo_version is not None
    assert rust_payload["project"]["version"] == root_payload["project"]["version"]
    assert cargo_version == root_payload["project"]["version"]


def test_release_workflow_builds_and_publishes_nexus_kernel() -> None:
    workflow_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "build-rust-wheel:" in workflow
    assert "build-rust-sdist:" in workflow
    assert "publish-rust:" in workflow
    assert (
        "maturin build --release --compatibility off --manifest-path rust/nexus-cdylib/Cargo.toml"
        in workflow
    )
    assert "maturin sdist --manifest-path rust/nexus-cdylib/Cargo.toml --out dist" in workflow

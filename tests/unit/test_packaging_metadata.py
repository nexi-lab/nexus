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

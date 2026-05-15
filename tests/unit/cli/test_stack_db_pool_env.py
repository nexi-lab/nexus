"""Compose stack DB pool defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _nexus_env(compose_file: Path) -> dict[str, Any]:
    stack = yaml.safe_load(compose_file.read_text())
    return stack["services"]["nexus"]["environment"]


def test_default_stack_uses_bounded_async_postgres_pool() -> None:
    env = _nexus_env(REPO_ROOT / "nexus-stack.yml")

    assert env["NEXUS_DB_POOL_SIZE"] == "${NEXUS_DB_POOL_SIZE:-5}"
    assert env["NEXUS_DB_MAX_OVERFLOW"] == "${NEXUS_DB_MAX_OVERFLOW:-5}"
    assert env["NEXUS_DB_ASYNC_USE_POOL"] == "${NEXUS_DB_ASYNC_USE_POOL:-1}"


def test_bundled_stack_uses_bounded_async_postgres_pool() -> None:
    env = _nexus_env(REPO_ROOT / "src/nexus/cli/data/nexus-stack.yml")

    assert env["NEXUS_DB_POOL_SIZE"] == "${NEXUS_DB_POOL_SIZE:-5}"
    assert env["NEXUS_DB_MAX_OVERFLOW"] == "${NEXUS_DB_MAX_OVERFLOW:-5}"
    assert env["NEXUS_DB_ASYNC_USE_POOL"] == "${NEXUS_DB_ASYNC_USE_POOL:-1}"

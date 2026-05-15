"""Architecture boundary checks for contract-layer helpers."""

from __future__ import annotations

import ast
from pathlib import Path


def test_agent_utils_does_not_import_storage_layer() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "src" / "nexus" / "contracts" / "agent_utils.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not any(module.startswith("nexus.storage") for module in imported_modules)

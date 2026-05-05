"""Regression checks for NexusFS temporary metastore creation."""

from __future__ import annotations

import ast
from pathlib import Path


def test_nexus_fs_does_not_use_tempfile_mktemp() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "src" / "nexus" / "core" / "nexus_fs.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    mktemp_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mktemp"
    ]

    assert mktemp_calls == []

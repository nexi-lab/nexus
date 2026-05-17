"""Extract DeploymentProfile enum values via AST."""

from __future__ import annotations

import ast
from pathlib import Path


def extract_profile_names(py_path: Path, *, enum_class: str) -> list[str]:
    """Extract string values from an enum class using AST.

    Args:
        py_path: Path to the Python file containing the enum.
        enum_class: Name of the enum class to extract from.

    Returns:
        Sorted list of enum string values.
    """
    tree = ast.parse(py_path.read_text())
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != enum_class:
            continue
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            if not isinstance(item.value, ast.Constant) or not isinstance(item.value.value, str):
                continue
            out.append(item.value.value)
    return sorted(out)

"""Extract public method names from SDK client classes via AST.

Public = doesn't start with underscore. Includes sync + async methods.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawSdkMethod:
    class_name: str
    method_name: str
    source: str  # "file.py:line"


def extract_sdk_methods(
    py_path: Path,
    *,
    class_names: tuple[str, ...],
) -> list[RawSdkMethod]:
    tree = ast.parse(py_path.read_text())
    out: list[RawSdkMethod] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in class_names:
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_"):
                continue
            out.append(
                RawSdkMethod(
                    class_name=node.name,
                    method_name=item.name,
                    source=f"{py_path}:{item.lineno}",
                )
            )
    return sorted(out, key=lambda r: r.method_name)

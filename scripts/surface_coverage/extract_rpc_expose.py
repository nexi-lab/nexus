"""Scan a source tree for @rpc_expose(name=..., description=...) decorators.

The decorator pattern is:
    @rpc_expose(name="oauth_list_providers", description="...")
    def method(self): ...

When `name=` is omitted, the method name itself is used.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawRpcExpose:
    name: str
    class_name: str
    method_name: str
    source: str  # "file.py:line"


def extract_rpc_exposes(root: Path) -> list[RawRpcExpose]:
    out: list[RawRpcExpose] = []
    for py in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        out.extend(_scan_module(tree, py))
    return sorted(out, key=lambda r: (r.name, r.source))


def _scan_module(tree: ast.AST, py: Path) -> list[RawRpcExpose]:
    out: list[RawRpcExpose] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for item in cls.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in item.decorator_list:
                name = _extract_rpc_expose_name(deco, item.name)
                if name is not None:
                    out.append(
                        RawRpcExpose(
                            name=name,
                            class_name=cls.name,
                            method_name=item.name,
                            source=f"{py}:{deco.lineno}",
                        )
                    )
    return out


def _extract_rpc_expose_name(deco: ast.AST, fallback_method_name: str) -> str | None:
    """Return the exposed name if `deco` is an @rpc_expose(...) call, else None."""
    if not isinstance(deco, ast.Call):
        return None
    callee = deco.func
    if (
        isinstance(callee, ast.Name)
        and callee.id == "rpc_expose"
        or isinstance(callee, ast.Attribute)
        and callee.attr == "rpc_expose"
    ):
        pass
    else:
        return None
    for kw in deco.keywords:
        if (
            kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return fallback_method_name

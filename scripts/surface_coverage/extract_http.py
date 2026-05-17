"""Extract HTTP routes from FastAPI files via AST.

v3: recursively scans server/api/ for any *.py files that register routes
via `@router.{get,post,put,patch,delete}('/path/...')` decorators.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@dataclass(frozen=True)
class RawHttpRoute:
    method: str
    path: str
    source: str


def extract_http_routes(py_path_or_dir: Path) -> list[RawHttpRoute]:
    """Accept a single file (legacy) or a directory (v3 recursive scan)."""
    out: list[RawHttpRoute] = []
    if py_path_or_dir.is_file():
        out.extend(_extract_from_file(py_path_or_dir))
    elif py_path_or_dir.is_dir():
        for py in sorted(py_path_or_dir.rglob("*.py")):
            out.extend(_extract_from_file(py))
    return sorted(out, key=lambda r: (r.path, r.method))


def _extract_from_file(py_path: Path) -> list[RawHttpRoute]:
    try:
        tree = ast.parse(py_path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []
    out: list[RawHttpRoute] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            route = _route_from_decorator(deco)
            if route is None:
                continue
            method, path = route
            out.append(
                RawHttpRoute(
                    method=method.upper(),
                    path=path,
                    source=f"{py_path}:{deco.lineno}",
                )
            )
    return out


def _route_from_decorator(deco: ast.AST) -> tuple[str, str] | None:
    if not isinstance(deco, ast.Call):
        return None
    if not isinstance(deco.func, ast.Attribute):
        return None
    if deco.func.attr not in _HTTP_METHODS:
        return None
    if not deco.args:
        return None
    first = deco.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    return (deco.func.attr, first.value)

"""Extract HTTP routes from FastAPI files via AST.

Recognizes @<obj>.{get,post,put,patch,delete}('/path/...') decorators
where <obj> is any router-like instance.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@dataclass(frozen=True)
class RawHttpRoute:
    method: str  # uppercase
    path: str
    source: str  # "file.py:line"


def extract_http_routes(py_path: Path) -> list[RawHttpRoute]:
    tree = ast.parse(py_path.read_text())
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
    return sorted(out, key=lambda r: (r.path, r.method))


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

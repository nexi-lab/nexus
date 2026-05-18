"""Extract HTTP routes from FastAPI files via AST.

v3: recursively scans server/api/ for any *.py files that register routes
via `@<router>.{get,post,put,patch,delete,head,options}('/path/...')` decorators.
Router prefixes from module-level `APIRouter(prefix="/api/v2/...")` assignments
are prepended so the recorded path matches the externally-visible route.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


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
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    prefixes = _collect_router_prefixes(tree)
    out: list[RawHttpRoute] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            route = _route_from_decorator(deco, prefixes)
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


def _collect_router_prefixes(tree: ast.AST) -> dict[str, str]:
    """Return {router_name: prefix} for module-level `<name> = APIRouter(prefix=...)`."""
    out: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        targets, value = _assignment_parts(node)
        if value is None:
            continue
        if not isinstance(value, ast.Call):
            continue
        callee = value.func
        if isinstance(callee, ast.Attribute):
            callee_name = callee.attr
        elif isinstance(callee, ast.Name):
            callee_name = callee.id
        else:
            continue
        if "APIRouter" not in callee_name and "Router" not in callee_name:
            continue
        prefix = _kwarg_str(value, "prefix")
        if prefix is None:
            continue
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                out[tgt.id] = prefix
    return out


def _assignment_parts(node: ast.AST) -> tuple[list[ast.AST], ast.AST | None]:
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target], node.value
    return [], None


def _kwarg_str(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if (
            kw.arg == name
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


def _route_from_decorator(deco: ast.AST, prefixes: dict[str, str]) -> tuple[str, str] | None:
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
    raw_path = first.value
    # Look up the router prefix if the decorator's object is a known router name
    router_name: str | None = None
    if isinstance(deco.func.value, ast.Name):
        router_name = deco.func.value.id
    full_path = raw_path
    if router_name and router_name in prefixes:
        prefix = prefixes[router_name].rstrip("/")
        if raw_path.startswith("/") or raw_path == "":
            full_path = f"{prefix}{raw_path}"
        else:
            full_path = f"{prefix}/{raw_path}"
        if full_path == "":
            full_path = prefix or "/"
    return (deco.func.attr, full_path)

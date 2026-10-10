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
    """Extract routes, composing router mounts within the scanned directory."""
    paths = (
        [py_path_or_dir]
        if py_path_or_dir.is_file()
        else sorted(py_path_or_dir.rglob("*.py"))
        if py_path_or_dir.is_dir()
        else []
    )
    trees: dict[Path, ast.Module] = {}
    for path in paths:
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
    routers = {
        (path, name): prefix
        for path, tree in trees.items()
        for name, prefix in _collect_router_prefixes(tree).items()
    }
    parents: dict[tuple[Path, str], list[tuple[tuple[Path, str], str]]] = {}
    for path, tree in trees.items():
        imports = _router_imports(path, tree, trees)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "include_router"
                and isinstance(node.func.value, ast.Name)
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                continue
            parent = (path, node.func.value.id)
            child = imports.get(node.args[0].id, (path, node.args[0].id))
            if parent in routers and child in routers:
                parents.setdefault(child, []).append((parent, _kwarg_str(node, "prefix") or ""))

    def prefixes(key: tuple[Path, str], visiting: frozenset = frozenset()) -> set[str]:
        if key in visiting:
            raise ValueError(f"Router include cycle at {key[0]}:{key[1]}")
        own = routers.get(key, "")
        if key not in parents:
            return {own}
        return {
            _join_path(_join_path(base, mount), own)
            for parent, mount in parents[key]
            for base in prefixes(parent, visiting | {key})
        }

    out: list[RawHttpRoute] = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                    continue
                obj = deco.func.value
                name = obj.id if isinstance(obj, ast.Name) else ""
                for prefix in prefixes((path, name)):
                    route = _route_from_decorator(deco, {name: prefix})
                    if route is not None:
                        method, full_path = route
                        out.append(RawHttpRoute(method.upper(), full_path, f"{path}:{deco.lineno}"))
    return sorted(out, key=lambda r: (r.path, r.method))


def _join_path(prefix: str, suffix: str) -> str:
    return prefix.rstrip("/") + ("/" if suffix and not suffix.startswith("/") else "") + suffix


def _router_imports(
    path: Path, tree: ast.Module, trees: dict[Path, ast.Module]
) -> dict[str, tuple[Path, str]]:
    by_path = {p.resolve(): p for p in trees}
    imports: dict[str, tuple[Path, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        parts = node.module.split(".")
        target = None
        if node.level:
            base = path.parent
            for _ in range(node.level - 1):
                base = base.parent
            target = by_path.get(base.joinpath(*parts).with_suffix(".py").resolve())
        else:
            target = next(
                (p for p in trees if p.with_suffix("").parts[-len(parts) :] == tuple(parts)), None
            )
        if target is not None:
            for alias in node.names:
                imports[alias.asname or alias.name] = (target, alias.name)
    return imports


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
        prefix = _kwarg_str(value, "prefix") or ""
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

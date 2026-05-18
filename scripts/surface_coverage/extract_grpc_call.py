"""Extract generic gRPC `Call` names from a dispatch declaration via AST.

The names live in src/nexus/server/_kernel_syscall_dispatch.py as a module-level
assignment. The shape may be either:

    DISPATCH = {"fs.read": _read_fn, ...}             # dict of name -> handler
    KERNEL_SYSCALL_NAMES = frozenset("fs.read", ...)  # frozenset of names

Both shapes are supported. String literals are the source of truth either way.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawGrpcCallName:
    name: str
    source: str  # "file.py:line"


def extract_grpc_call_names(
    py_path: Path,
    *,
    dispatch_var: str = "DISPATCH",
) -> list[RawGrpcCallName]:
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        value = _value_for_target(node, dispatch_var)
        if value is None:
            continue
        return _names_from_value(value, py_path)
    raise ValueError(f"variable {dispatch_var!r} not found in {py_path}")


def _value_for_target(node: ast.AST, var_name: str) -> ast.AST | None:
    """Return the value node if `node` assigns to `var_name`; else None."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == var_name:
                return node.value
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == var_name
        and node.value is not None
    ):
        return node.value
    return None


def _names_from_value(value: ast.AST, py_path: Path) -> list[RawGrpcCallName]:
    """Extract string-literal names from a dict literal OR a frozenset(...) call."""
    if isinstance(value, ast.Dict):
        return _names_from_dict(value, py_path)
    if isinstance(value, ast.Call) and _is_frozenset_call(value.func):
        return _names_from_frozenset_call(value, py_path)
    raise ValueError(
        "expected dict literal or frozenset(...) call; "
        f"got {type(value).__name__} at {py_path}:{getattr(value, 'lineno', '?')}"
    )


def _is_frozenset_call(func: ast.AST) -> bool:
    return (isinstance(func, ast.Name) and func.id == "frozenset") or (
        isinstance(func, ast.Attribute) and func.attr == "frozenset"
    )


def _names_from_dict(value: ast.Dict, py_path: Path) -> list[RawGrpcCallName]:
    out: list[RawGrpcCallName] = []
    for k in value.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.append(RawGrpcCallName(name=k.value, source=f"{py_path}:{k.lineno}"))
    return sorted(out, key=lambda r: r.name)


def _names_from_frozenset_call(call: ast.Call, py_path: Path) -> list[RawGrpcCallName]:
    """`frozenset("a", "b", ...)` OR `frozenset(["a", "b", ...])` OR
    `frozenset({"a", "b", ...})`."""
    out: list[RawGrpcCallName] = []
    # collect all string-literal args, plus elts of any iterable arg
    candidates: list[ast.AST] = list(call.args)
    extra_elts: list[ast.AST] = []
    for arg in call.args:
        if isinstance(arg, (ast.List, ast.Tuple, ast.Set)):
            extra_elts.extend(arg.elts)
    candidates.extend(extra_elts)
    for c in candidates:
        if isinstance(c, ast.Constant) and isinstance(c.value, str):
            out.append(RawGrpcCallName(name=c.value, source=f"{py_path}:{c.lineno}"))
    # dedupe by name (frozenset semantics) preserving first occurrence
    seen: set[str] = set()
    unique: list[RawGrpcCallName] = []
    for entry in out:
        if entry.name in seen:
            continue
        seen.add(entry.name)
        unique.append(entry)
    return sorted(unique, key=lambda r: r.name)
